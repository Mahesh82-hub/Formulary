"""Drug-name resolution through RxNorm, the National Library of Medicine's drug vocabulary.

FDA data uses United States Adopted Names. Where a user's name differs - an international
name such as "paracetamol" for acetaminophen - a search for it finds nothing, and the
assistant once reported on that basis that FDA had no data. A hand-typed table of 34 name
pairs fixed that example but failed silently for every drug it did not list. RxNorm covers
every drug, including international names through the WHO ATC vocabulary it incorporates.

Only true synonyms are expanded: an ingredient searched under an international name is also
searched under its US name. A brand is never widened to its ingredient - a question about
Tylenol Extra Strength must not become a search across all 3,500 acetaminophen labels.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from functools import lru_cache
from typing import Any, Protocol

import httpx

from app.core.config import get_settings
from app.sources.profile import RXNORM_PROFILE
from app.sources.resilience import ResilientRequester, UpstreamError

# RxNorm term types for ingredients (single, precise, and multiple ingredients).
INGREDIENT_TERM_TYPES = frozenset({"IN", "PIN", "MIN"})
MAX_CACHE_ENTRIES = 5_000
MAX_TERM_LENGTH = 100


class DrugNameResolver(Protocol):
    async def variants(self, term: str) -> tuple[str, ...]:
        """Every name worth searching for a drug, starting with the term as given."""
        ...


def _clean(term: str) -> str:
    return " ".join(term.split())[:MAX_TERM_LENGTH]


async def expand(resolver: DrugNameResolver, terms: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """Resolve several terms concurrently."""
    unique = list(dict.fromkeys(_clean(term) for term in terms if term and term.strip()))
    resolved = await asyncio.gather(*(resolver.variants(term) for term in unique))
    return dict(zip(unique, resolved, strict=True))


class PassthroughNames:
    """No expansion: used when RxNorm is disabled and in tests that do not need it."""

    async def variants(self, term: str) -> tuple[str, ...]:
        cleaned = _clean(term)
        return (cleaned,) if cleaned else ()


class StaticNames:
    """Fixed synonyms for tests."""

    def __init__(self, synonyms: dict[str, str]) -> None:
        self._synonyms = {key.casefold(): value for key, value in synonyms.items()}

    async def variants(self, term: str) -> tuple[str, ...]:
        cleaned = _clean(term)
        if not cleaned:
            return ()
        twin = self._synonyms.get(cleaned.casefold())
        return (cleaned, twin) if twin else (cleaned,)


class RxNormNames:
    """Resolves names against RxNav, caching results and de-duplicating concurrent lookups."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
        requester: ResilientRequester | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport
        self._requester = requester or RXNORM_PROFILE.build_requester()
        self._cache: dict[str, tuple[str, ...]] = {}
        self._inflight: dict[str, asyncio.Task[tuple[str, ...]]] = {}

    async def variants(self, term: str) -> tuple[str, ...]:
        cleaned = _clean(term)
        if not cleaned:
            return ()
        key = cleaned.casefold()
        if key in self._cache:
            return self._cache[key]
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._resolve(cleaned))
            self._inflight[key] = task
        try:
            return await task
        finally:
            self._inflight.pop(key, None)

    async def _resolve(self, term: str) -> tuple[str, ...]:
        try:
            rxcui = await self._exact_concept(term)
            if rxcui is None:
                result: tuple[str, ...] = (term,)
            else:
                properties = await self._get(f"rxcui/{rxcui}/properties.json", {})
                concept = properties.get("properties") or {}
                name = concept.get("name")
                is_ingredient = concept.get("tty") in INGREDIENT_TERM_TYPES
                if is_ingredient and isinstance(name, str) and name.casefold() != term.casefold():
                    result = (term, name)
                else:
                    result = (term,)
        except (UpstreamError, httpx.HTTPError, ValueError):
            # Resolution is an enrichment: without it the term is still searched as given.
            # A failure is not cached, so the next request tries again.
            return (term,)
        if len(self._cache) < MAX_CACHE_ENTRIES:
            self._cache[term.casefold()] = result
        return result

    async def _exact_concept(self, term: str) -> str | None:
        """The concept a term unambiguously names, or None.

        RxNorm's approximate search always returns something, so a match is trusted only when
        a candidate's name equals the term exactly, or when every candidate is the same
        ingredient concept - a spelling variant such as "aciclovir" for acyclovir. Following
        candidates to their ingredients is deliberately avoided: every candidate for "Bayer"
        is a Bayer Aspirin product, which would turn a company name into "aspirin".
        """
        payload = await self._get("approximateTerm.json", {"term": term, "maxEntries": "8"})
        candidates = (payload.get("approximateGroup") or {}).get("candidate") or []
        for candidate in candidates:
            name = candidate.get("name")
            if isinstance(name, str) and name.casefold() == term.casefold():
                rxcui = candidate.get("rxcui")
                return str(rxcui) if rxcui else None
        concepts = {str(candidate["rxcui"]) for candidate in candidates if candidate.get("rxcui")}
        if len(concepts) == 1:
            (only,) = concepts
            properties = await self._get(f"rxcui/{only}/properties.json", {})
            if (properties.get("properties") or {}).get("tty") in INGREDIENT_TERM_TYPES:
                return only
        return None

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            request = client.build_request("GET", f"{self._base_url}/{path}", params=params)
            response = await self._requester.send(client, request)
        response.raise_for_status()
        payload: object = response.json()
        if not isinstance(payload, dict):
            raise ValueError("RxNorm returned an unexpected response")
        return payload


@lru_cache
def get_name_resolver() -> DrugNameResolver:
    settings = get_settings()
    if not settings.rxnorm_enabled:
        return PassthroughNames()
    return RxNormNames(
        base_url=settings.rxnorm_base_url,
        timeout_seconds=settings.rxnorm_timeout_seconds,
        requester=RXNORM_PROFILE.with_overrides(
            timeout_seconds=settings.rxnorm_timeout_seconds
        ).build_requester(),
    )

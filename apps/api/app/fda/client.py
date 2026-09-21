from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, cast

import httpx

from app.core.config import get_settings
from app.fda.models import (
    OPENFDA_DATASETS,
    OPENFDA_MAX_SKIP,
    FDAProvenance,
    OpenFDADataset,
    OpenFDAQuery,
    OpenFDAResult,
)
from app.sources.profile import OPENFDA_PROFILE
from app.sources.resilience import (
    ResilientRequester,
    UpstreamRateLimitError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)


class OpenFDAError(RuntimeError):
    """Safe application error raised for an unsuccessful openFDA request."""


class OpenFDAClient:
    """Read-only client for the allowlisted openFDA Elasticsearch API surface."""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        timeout_seconds: float,
        max_records: int,
        transport: httpx.AsyncBaseTransport | None = None,
        requester: ResilientRequester | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_records = max_records
        self._transport = transport
        self._requester = requester or OPENFDA_PROFILE.build_requester()

    async def query(
        self,
        dataset: OpenFDADataset,
        *,
        search: str | None = None,
        count: str | None = None,
        sort: str | None = None,
        limit: int = 5,
        skip: int = 0,
    ) -> OpenFDAResult:
        self._validate_query(
            dataset=dataset,
            search=search,
            count=count,
            sort=sort,
            limit=limit,
            skip=skip,
        )
        query = OpenFDAQuery(
            dataset=dataset,
            search=search,
            count=count,
            sort=sort,
            limit=limit,
            skip=skip,
        )
        public_params = self._query_params(query)
        request_params: list[tuple[str, str | int | float | bool | None]] = list(public_params)
        if self._api_key:
            request_params.insert(0, ("api_key", self._api_key))

        endpoint_url = f"{self._base_url}/{dataset}.json"
        api_url = str(httpx.URL(endpoint_url, params=public_params))
        # One client per query is reused across retry attempts so a retried request does not
        # pay for a fresh TLS handshake.
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                request = client.build_request("GET", endpoint_url, params=request_params)
                response = await self._requester.send(client, request)
        except UpstreamTimeoutError as error:
            raise OpenFDAError("openFDA timed out") from error
        except UpstreamRateLimitError as error:
            raise OpenFDAError("openFDA rate limit reached") from error
        except UpstreamUnavailableError as error:
            raise OpenFDAError("openFDA could not be reached") from error

        if response.status_code == 404:
            return self._empty_result(query, api_url)
        if response.status_code == 429:
            raise OpenFDAError("openFDA rate limit reached")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise OpenFDAError(f"openFDA returned HTTP {error.response.status_code}") from error

        try:
            payload: object = response.json()
        except ValueError as error:
            raise OpenFDAError("openFDA returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise OpenFDAError("openFDA returned an invalid response")

        data = cast(dict[str, Any], payload)
        records = self._records(data.get("results"))
        meta = data.get("meta")
        meta_object = cast(dict[str, Any], meta) if isinstance(meta, dict) else {}
        result_meta = meta_object.get("results")
        result_meta_object = (
            cast(dict[str, Any], result_meta) if isinstance(result_meta, dict) else {}
        )
        total_value = result_meta_object.get("total")
        total = total_value if isinstance(total_value, int) else None

        return OpenFDAResult(
            query=query,
            total=total,
            returned=len(records),
            results=records,
            provenance=FDAProvenance(
                api_url=api_url,
                retrieved_at=datetime.now(UTC),
                dataset_last_updated=self._optional_string(meta_object.get("last_updated")),
                disclaimer=self._optional_string(meta_object.get("disclaimer")),
                license_url=self._optional_string(meta_object.get("license")),
                terms_url=self._optional_string(meta_object.get("terms")),
                next_page_url=self._sanitize_link(response.headers.get("link")),
            ),
        )

    def _validate_query(
        self,
        *,
        dataset: str,
        search: str | None,
        count: str | None,
        sort: str | None,
        limit: int,
        skip: int,
    ) -> None:
        if dataset not in OPENFDA_DATASETS:
            raise OpenFDAError("Unsupported openFDA dataset")
        if limit < 1 or limit > self._max_records:
            raise OpenFDAError(f"openFDA limit must be between 1 and {self._max_records}")
        if skip < 0 or skip > OPENFDA_MAX_SKIP:
            raise OpenFDAError(f"openFDA skip must be between 0 and {OPENFDA_MAX_SKIP}")
        for name, value in (("search", search), ("count", count), ("sort", sort)):
            if value is None:
                continue
            if not value.strip() or len(value) > 2_000 or any(ord(char) < 32 for char in value):
                raise OpenFDAError(f"Invalid openFDA {name} expression")
            if "api_key" in value.casefold() or "://" in value:
                raise OpenFDAError(f"Invalid openFDA {name} expression")

    @staticmethod
    def _query_params(query: OpenFDAQuery) -> list[tuple[str, str]]:
        params: list[tuple[str, str]] = []
        if query.search:
            params.append(("search", query.search))
        if query.count:
            params.append(("count", query.count))
        if query.sort:
            params.append(("sort", query.sort))
        params.append(("limit", str(query.limit)))
        if query.skip:
            params.append(("skip", str(query.skip)))
        return params

    @staticmethod
    def _records(value: object) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise OpenFDAError("openFDA response is missing results")
        records: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                raise OpenFDAError("openFDA returned malformed results")
            records.append(cast(dict[str, Any], item))
        return records

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return value if isinstance(value, str) else None

    @staticmethod
    def _sanitize_link(link_header: str | None) -> str | None:
        if not link_header or "<" not in link_header or ">" not in link_header:
            return None
        raw_url = link_header.split("<", 1)[1].split(">", 1)[0]
        try:
            url = httpx.URL(raw_url)
        except httpx.InvalidURL:
            return None
        params = [(key, value) for key, value in url.params.multi_items() if key != "api_key"]
        return str(url.copy_with(params=params))

    @staticmethod
    def _empty_result(query: OpenFDAQuery, api_url: str) -> OpenFDAResult:
        return OpenFDAResult(
            query=query,
            total=0,
            returned=0,
            results=[],
            provenance=FDAProvenance(
                api_url=api_url,
                retrieved_at=datetime.now(UTC),
            ),
        )


@lru_cache
def get_openfda_client() -> OpenFDAClient:
    settings = get_settings()
    api_key = settings.openfda_api_key.get_secret_value() if settings.openfda_api_key else None
    profile = OPENFDA_PROFILE.with_overrides(
        rate_per_second=settings.openfda_rate_limit_per_second,
        burst=settings.openfda_rate_limit_burst,
        max_attempts=settings.openfda_max_attempts,
        base_delay_seconds=settings.openfda_retry_base_delay_seconds,
        max_delay_seconds=settings.openfda_retry_max_delay_seconds,
        timeout_seconds=settings.openfda_timeout_seconds,
    )
    return OpenFDAClient(
        api_key=api_key,
        base_url=settings.openfda_base_url,
        timeout_seconds=settings.openfda_timeout_seconds,
        max_records=settings.openfda_tool_max_records,
        # The requester holds the shared token bucket, so it must outlive individual calls.
        requester=profile.build_requester(),
    )

"""Turns the evidence tools returned into the sources an answer actually used.

Two rules, both learned from live answers:

* **List what was used, not what was retrieved.** An earlier version appended every record
  any tool returned. A pKa question then listed protein kinase A papers, because keyword
  search matched "PKA", and a ceftiofur answer listed a cefoperazone paediatric study. A source is
  now listed only when the answer refers to it - by URL, PubMed ID, trial ID, label set ID,
  or product name.
* **Never make an unverified citation look verified.** The model writes markers such as
  ``【pubmed:8234164】``. A marker whose identifier a tool actually returned becomes a link;
  any other is labelled unverified instead of being turned into a convincing-looking link.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

MAX_SOURCES = 12
FALLBACK_SOURCES = 3
DAILYMED_URL = "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={}"
PUBMED_URL = "https://pubmed.ncbi.nlm.nih.gov/{}/"
TRIAL_URL = "https://clinicaltrials.gov/study/{}"

PMID_IN_TEXT = re.compile(r"(?:pubmed[:\s#]*|pmid[:\s#]*)(\d{6,9})", re.IGNORECASE)
NCT_IN_TEXT = re.compile(r"\bNCT\d{8}\b", re.IGNORECASE)
# Bracketed identifier markers the model invents, e.g. 【pubmed:123】 or 【NCT01234567】.
ID_MARKER = re.compile(
    r"【\s*(?:(pubmed|pmid)\s*[:#]?\s*(\d{6,9})|(?:nct\s*[:#]?\s*)?(NCT\d{8}))\s*】", re.I
)


@dataclass
class SourceLink:
    title: str
    url: str
    # Identifiers that, if present in the answer, show the answer used this source.
    keys: set[str] = field(default_factory=set)
    # Names that count only when they appear as a whole word (brand names, headlines).
    names: set[str] = field(default_factory=set)
    tool: str = ""


def normalize(text: str) -> str:
    """Casefold and flatten typographic variants so quoted titles still match."""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[‐-―−]", "-", text)
    text = text.replace(" ", " ")
    return " ".join(text.casefold().split())


def _valid_url(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    parsed = urlparse(url)
    return url if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _link(
    title: object, url: object, *, keys: set[str] | None = None, names: set[str] | None = None
) -> SourceLink | None:
    valid = _valid_url(url)
    if valid is None:
        return None
    label = title if isinstance(title, str) and title.strip() else urlparse(valid).netloc
    all_keys = {normalize(valid)} | {normalize(key) for key in keys or set() if key}
    return SourceLink(
        title=" ".join(label.split())[:200],
        url=valid,
        keys=all_keys,
        names={normalize(name) for name in names or set() if name and len(name.strip()) >= 4},
    )


def _record_keys(external_key: object, url: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(external_key, str):
        pubmed = re.match(r"pubmed:(\d+)", external_key)
        if pubmed:
            keys |= {f"pubmed:{pubmed.group(1)}", f"pmid {pubmed.group(1)}", pubmed.group(1)}
        trial = re.match(r"nct:(NCT\d{8})", external_key, re.I)
        if trial:
            keys.add(trial.group(1))
    if isinstance(url, str):
        set_id = re.search(r"setid=([0-9a-f-]{36})", url)
        if set_id:
            keys.add(set_id.group(1))
    return keys


def citable_links(tool_name: str, data: Any) -> list[SourceLink]:
    """The official records behind a tool result, most relevant first."""
    links = _citable_links(tool_name, data)
    for link in links:
        link.tool = tool_name
    return links


def _citable_links(tool_name: str, data: Any) -> list[SourceLink]:
    if not isinstance(data, dict):
        return []
    links: list[SourceLink | None] = []
    if tool_name == "get_drug_composition":
        for maker in data.get("manufacturers") or []:
            for product in (maker or {}).get("products") or []:
                brand = product.get("brand_name")
                links.append(
                    _link(
                        f"{brand or 'Product'} - {maker.get('manufacturer')} (DailyMed label)",
                        product.get("label_url"),
                        keys={product.get("set_id") or ""},
                        names={brand or ""},
                    )
                )
    elif tool_name == "search_all_sources":
        for record in data.get("records") or []:
            if not isinstance(record, dict):
                continue
            url = record.get("url") or (record.get("provenance") or {}).get("api_url")
            links.append(
                _link(
                    f"{record.get('title')} ({record.get('source')})",
                    url,
                    keys=_record_keys(record.get("external_key"), url)
                    | {str(record.get("title") or "")[:60]},
                )
            )
    elif tool_name == "search_regulatory_events":
        for event in data.get("events") or []:
            if isinstance(event, dict):
                links.append(
                    _link(
                        event.get("headline"),
                        event.get("source_url"),
                        names={event.get("subject") or "", *(event.get("drug_names") or [])},
                    )
                )
    elif isinstance(data.get("results"), list) and isinstance(data.get("provenance"), dict):
        for record in data["results"]:
            if not isinstance(record, dict) or not isinstance(record.get("set_id"), str):
                continue
            raw_openfda = record.get("openfda")
            openfda: dict[str, Any] = raw_openfda if isinstance(raw_openfda, dict) else {}
            names = [*(openfda.get("brand_name") or []), *(openfda.get("generic_name") or [])]
            maker = (openfda.get("manufacturer_name") or [None])[0]
            title = " - ".join(part for part in (names[0] if names else "FDA label", maker) if part)
            links.append(
                _link(
                    f"{title} (DailyMed label)",
                    DAILYMED_URL.format(record["set_id"]),
                    keys={record["set_id"]},
                    names=set(names[:3]),
                )
            )
    resolved = [link for link in links if link is not None]
    if (
        not resolved
        and isinstance(data.get("provenance"), dict)
        and (data.get("returned") or data.get("results"))
    ):
        dataset = (data.get("query") or {}).get("dataset", "openFDA")
        query_link = _link(f"openFDA {dataset} query", data["provenance"].get("api_url"))
        if query_link:
            resolved.append(query_link)
    return resolved


def merge(existing: list[SourceLink], new: list[SourceLink]) -> None:
    for link in new:
        if len(existing) >= MAX_SOURCES * 4:
            return
        match = next((item for item in existing if item.url == link.url), None)
        if match is None:
            existing.append(link)
        else:
            match.keys |= link.keys
            match.names |= link.names


def _used(link: SourceLink, answer: str) -> bool:
    if any(key and key in answer for key in link.keys if len(key) >= 6):
        return True
    return any(re.search(rf"(?<!\w){re.escape(name)}(?!\w)", answer) for name in link.names)


def link_inline_identifiers(text: str, links: list[SourceLink]) -> str:
    """Rewrite identifier markers as links, but only for identifiers a tool returned."""
    known = {key for link in links for key in link.keys}

    def replace(match: re.Match[str]) -> str:
        pmid, trial = match.group(2), match.group(3)
        if pmid:
            if pmid in known:
                return f" ([PMID {pmid}]({PUBMED_URL.format(pmid)}))"
            return f" (PMID {pmid}, unverified: not returned by any source this turn)"
        assert trial is not None
        trial = trial.upper()
        if normalize(trial) in known:
            return f" ([{trial}]({TRIAL_URL.format(trial)}))"
        return f" ({trial}, unverified: not returned by any source this turn)"

    return ID_MARKER.sub(replace, text)


ANY_MARKER = re.compile(r"【([^】]*)】")
UNTERMINATED_MARKER = re.compile(r"[ \t]*【[^】\w\n|]*")
FAMILY_HOSTS = {
    "openfda": ("api.fda.gov", "dailymed.nlm.nih.gov", "accessdata.fda.gov"),
    "fda": ("api.fda.gov", "dailymed.nlm.nih.gov", "accessdata.fda.gov", "fda.gov"),
    "dailymed": ("dailymed.nlm.nih.gov",),
    "label": ("dailymed.nlm.nih.gov",),
    "pubmed": ("pubmed.ncbi.nlm.nih.gov",),
    "clinicaltrials": ("clinicaltrials.gov",),
    "trial": ("clinicaltrials.gov",),
}


def resolve_remaining_markers(text: str, links: list[SourceLink]) -> str:
    """Turn any other bracketed marker into a link, or remove it.

    Models cite in whatever form they have seen: tool names ("【openfda】",
    "【analyze_fda_adverse_event_reactions†L1-L9】") as readily as identifiers. A marker that
    names a tool or a source family resolves to the record that tool returned; anything
    unrecognisable is removed rather than shown as noise.
    """

    def replace(match: re.Match[str]) -> str:
        label = normalize(match.group(1)).split("†", 1)[0].strip()
        for link in links:
            if link.tool and link.tool in label:
                return f" ([{urlparse(link.url).netloc}]({link.url}))"
        for family, hosts in FAMILY_HOSTS.items():
            if family in label:
                for link in links:
                    if any(host in link.url for host in hosts):
                        return f" ([{urlparse(link.url).netloc}]({link.url}))"
        return "\u0000"

    # A removed marker takes its leading space with it, so "a claim 【x】." reads "a claim.".
    text = re.sub(r"[ \t]*\u0000", "", ANY_MARKER.sub(replace, text))
    # An opening bracket that never closes is an output artifact ("【 } |"); drop it and the
    # stray punctuation attached to it.
    # Never swallow a table pipe that follows the artifact; keep one space before it.
    return UNTERMINATED_MARKER.sub(
        lambda match: " " if match.string[match.end() : match.end() + 1] == "|" else "", text
    )


def finalize_answer(text: str, links: list[SourceLink]) -> str:
    """Link inline identifiers and append the sources the answer actually used."""
    text = resolve_remaining_markers(link_inline_identifiers(text, links), links)
    answer = normalize(text)
    used = [link for link in links if _used(link, answer)]
    heading = "**Sources**"
    if not used and links:
        # Nothing was referenced explicitly. Be honest that these were consulted, not cited.
        used, heading = links[:FALLBACK_SOURCES], "**Sources consulted**"
    remaining = [link for link in used if link.url not in text][:MAX_SOURCES]
    if not remaining:
        return text
    lines = ["", "", heading]
    lines += [f"{number}. [{link.title}]({link.url})" for number, link in enumerate(remaining, 1)]
    return text + "\n".join(lines)

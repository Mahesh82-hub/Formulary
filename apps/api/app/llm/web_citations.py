"""Turns Groq built-in browsing output into real, clickable citations.

GPT-OSS models cite browsed pages with markers such as ``【1†L119-L125】``, where the number is
the index of the browsing step (a search or an opened page) and the rest is a line range on
that page. Those markers are meaningless to a reader. This module maps each marker to the URL
of the page it refers to, rewrites it as a markdown link, and returns the pages that were
cited so the answer can carry a source list that does not depend on the model remembering to
write one. Structure was established from live responses in September 2026:

* ``executed_tools[i]`` has ``type`` (``browser_search`` or ``browser.open``), ``arguments``
  (a JSON string), and ``search_results.results`` - a list of ``{title, url, content, score}``.
* An open step's arguments are ``{"cursor": c, "id": n}``: result ``n`` of step ``c``.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from app.llm.models import WebSource

CITATION_MARKER = re.compile(r"【(\d+)†[^】]*】")
# Browsing pages are titled like "example.com - viewing lines [0 - 172] of 172".
VIEWING_SUFFIX = re.compile(r"\s+-\s+viewing lines.*$", re.IGNORECASE)


def _results(tool: dict[str, Any]) -> list[dict[str, Any]]:
    search_results = tool.get("search_results")
    if not isinstance(search_results, dict):
        return []
    results = search_results.get("results")
    return [item for item in results if isinstance(item, dict)] if isinstance(results, list) else []


def _arguments(tool: dict[str, Any]) -> dict[str, Any]:
    raw = tool.get("arguments")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _safe_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    # Only real web pages become links; the provider's own search-engine URL is not a source.
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or "exa.ai" in parsed.netloc:
        return None
    return value


def parse_executed_tools(
    executed: object,
) -> tuple[list[str], dict[int, WebSource]]:
    """Return the search queries run and the page each browsing step refers to."""
    if not isinstance(executed, list):
        return [], {}
    steps = [item for item in executed if isinstance(item, dict)]
    queries: list[str] = []
    pages: dict[int, WebSource] = {}
    for position, tool in enumerate(steps):
        raw_index = tool.get("index")
        index: int = raw_index if isinstance(raw_index, int) else position
        arguments = _arguments(tool)
        kind = str(tool.get("type") or tool.get("name") or "")
        if "search" in kind and "open" not in kind:
            query = arguments.get("query")
            if isinstance(query, str) and query.strip():
                queries.append(query.strip())
            continue
        # An opened page: prefer the title of the search result it was opened from.
        title: str | None = None
        url: str | None = None
        cursor, result_id = arguments.get("cursor"), arguments.get("id")
        if isinstance(cursor, int) and isinstance(result_id, int) and 0 <= cursor < len(steps):
            origin = _results(steps[cursor])
            if 0 <= result_id < len(origin):
                title = origin[result_id].get("title") or None
                url = _safe_url(origin[result_id].get("url"))
        own = _results(tool)
        if url is None and own:
            url = _safe_url(own[0].get("url"))
            title = title or own[0].get("title")
        if url is None and isinstance(result_id, str):
            url = _safe_url(result_id)
        if url is None:
            continue
        clean_title = VIEWING_SUFFIX.sub("", str(title or "")).strip()
        if not clean_title or clean_title == urlparse(url).netloc:
            clean_title = _page_title(tool.get("output")) or urlparse(url).netloc
        pages[index] = WebSource(title=clean_title[:200], url=url)
    return queries, pages


def _page_title(output: object) -> str | None:
    """The first line of an opened page that is neither blank nor its URL.

    Opened pages are rendered as numbered lines ("L3: DailyMed - TYLENOL ..."); when the
    browsing step did not come from a titled search result, this is the page's own heading.
    """
    if not isinstance(output, str):
        return None
    for raw in output.splitlines()[:12]:
        line = re.sub(r"^L\d+:\s*", "", raw).strip()
        if not line or line.upper().startswith("URL:") or re.match(r"https?://", line):
            continue
        return line.lstrip("#").strip()[:200] or None
    return None


MAX_EXCERPT_CHARACTERS = 8_000
MAX_EXCERPTS = 8


def page_excerpts(executed: object) -> list[str]:
    """What the browser actually read, kept so web-derived claims can be audited later."""
    if not isinstance(executed, list):
        return []
    excerpts: list[str] = []
    # Every browsing step, not only opened pages: the model also reads search snippets and
    # "find" results within a page, and a value it quotes may come from any of them.
    for tool in executed:
        if not isinstance(tool, dict):
            continue
        output = tool.get("output")
        if isinstance(output, str) and output.strip():
            excerpts.append(re.sub(r"(?m)^L\d+:\s?", "", output)[:MAX_EXCERPT_CHARACTERS])
        if len(excerpts) >= MAX_EXCERPTS:
            break
    return excerpts


def rewrite_citations(text: str, pages: dict[int, WebSource]) -> tuple[str, list[WebSource]]:
    """Replace browsing markers with markdown links; return the cited pages in order."""
    cited: list[WebSource] = []

    def replace(match: re.Match[str]) -> str:
        page = pages.get(int(match.group(1)))
        if page is None:
            return ""
        if all(existing.url != page.url for existing in cited):
            cited.append(page)
        return f" ([{urlparse(page.url).netloc}]({page.url}))"

    rewritten = CITATION_MARKER.sub(replace, text)
    # Adjacent markers collapse to repeated links; keep one.
    rewritten = re.sub(r"( \(\[[^\]]+\]\([^)]+\)\))(\1)+", r"\1", rewritten)
    return rewritten, cited


def sources_section(sources: list[WebSource]) -> str:
    lines = ["", "", "**Web sources**"]
    lines += [
        f"{number}. [{source.title}]({source.url})" for number, source in enumerate(sources, 1)
    ]
    lines.append(
        "_Found by web search. Where these differ from the official records cited above, the "
        "official records take precedence._"
    )
    return "\n".join(lines)

"""Finds figures in an answer that no retrieved source supports.

Written after a live answer tabulated plasma protein binding for cattle as "45-55 %" while
admitting the values were "not visible in the abstract snippets". Every figure is checked
against the text the tools actually returned. A figure counts as grounded when it appears
there directly, or through a transformation a careful scientist would make:

* rounding (0.844 reported as 0.84 or 0.8);
* unit scaling by 10^3 or 10^2 (0.0601 mg/mL as 60 ug/mL; a fraction as a percentage);
* complements (46.6 % bound reported as 53.4 % unbound, or fraction 0.534).

"Approximately" is deliberately not an excuse: approximating is the failure being caught. A
line showing explicit arithmetic ("=") is treated as a calculation, provided it builds on at
least one grounded figure. pH values are treated as conditions chosen for the question rather
than data, and small integers are ignored because they are almost always counts or labels.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

# Thousands separators are part of one number: "378,140" is 378140, not 378 and 140.
NUMBER = re.compile(r"(?<![\w.,])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\w])")
SOURCE_SECTIONS = re.compile(r"\n\s*\*\*(?:Sources|Web sources|Sources consulted)\*\*", re.I)
STRIP = [
    re.compile(r"https?://\S+"),
    re.compile(r"【[^】]*】"),
    re.compile(r"\((?:\[)?PMID[^)]*\)", re.I),
    re.compile(r"\b(?:PMID|PubMed)[:\s#]*\d{6,9}\b", re.I),
    re.compile(r"\bNCT\d{8}\b", re.I),
    re.compile(r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b"),
    re.compile(r"\b(?:NDA|ANDA|BLA)\s?\d{5,6}\b", re.I),
]
# "pH 7.4", "pH ≈ 6.8", and the upper end of a range such as "pH 6.8-7.4" or "pH 5 to 7".
PH_VALUE = re.compile(r"\bpH\s*[≈~=]?\s*(?:\d+(?:\.\d+)?\s*(?:[-–—−]|to|,|and|or)\s*)*$", re.I)
# Numbers that are part of a name rather than a quantity: "FD&C Red No. 40", "Yellow #5".
IDENTIFIER_BEFORE = re.compile(
    r"(?:\bNo\.|#|\b(?:FD&C|D&C)\s+[A-Za-z]+(?:\s+No\.)?|\b(?:Red|Yellow|Blue|Green))\s*$",
    re.I,
)
SCALES = (1.0, 1_000.0, 0.001, 100.0, 0.01)


# Three letters minimum, so technical terms such as pKa and AUC take part in matching.
WORD = re.compile(r"[a-z][a-z0-9\-]{2,}")
SHORT_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "are",
        "was",
        "not",
        "but",
        "per",
        "via",
        "all",
        "any",
        "its",
        "has",
        "had",
        "can",
        "may",
        "use",
        "one",
        "two",
    }
)
# Temperatures and times qualify a value rather than being values themselves.
CONDITION_AFTER = re.compile(r"^\s*(?:°\s*[CF]|degrees|deg\b)", re.IGNORECASE)
CONDITION_HEADER = re.compile(r"\b(?:ph|temperature|temp|time|day|week|dose level)\b", re.I)
CONTEXT_STOPWORDS = frozenset(
    {
        "about",
        "above",
        "abstract",
        "after",
        "also",
        "among",
        "approximately",
        "around",
        "article",
        "based",
        "being",
        "below",
        "between",
        "both",
        "cells",
        "column",
        "could",
        "data",
        "derived",
        "described",
        "does",
        "each",
        "either",
        "from",
        "full",
        "have",
        "into",
        "less",
        "level",
        "levels",
        "listed",
        "more",
        "most",
        "much",
        "must",
        "near",
        "only",
        "other",
        "over",
        "pubmed",
        "range",
        "ranges",
        "reported",
        "result",
        "results",
        "same",
        "shown",
        "some",
        "source",
        "sources",
        "studies",
        "study",
        "such",
        "table",
        "text",
        "than",
        "that",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "under",
        "value",
        "values",
        "very",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "with",
        "within",
        "would",
    }
)
WINDOW = 80
COMPACT_EVIDENCE_CHARACTERS = 1_500


def _value(token: str) -> float:
    return float(token.replace(",", ""))


def _decimals(token: str) -> int:
    return len(token.split(".", 1)[1]) if "." in token else 0


def _clean(text: str) -> str:
    for pattern in STRIP:
        text = pattern.sub(" ", text)
    return text


STEM_LENGTH = 6


INVISIBLE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")


def _normalize(text: str) -> str:
    """Flatten invisible characters and sub/superscripts: "pK\u200ba\u2082" reads "pka2"."""
    return unicodedata.normalize("NFKC", INVISIBLE.sub("", text)).casefold()


def _context_words(line: str) -> set[str]:
    """Word stems, so "solubility" matches "solubilities" and "pKa2" matches "pKa"."""
    stems = set()
    for word in WORD.findall(_normalize(line)):
        if word in CONTEXT_STOPWORDS or word in SHORT_STOPWORDS:
            continue
        stem = word.rstrip("0123456789")[:STEM_LENGTH]
        if len(stem) >= 3:
            stems.add(stem)
    return stems


class Evidence:
    """Numbers in the retrieved text, each with the words around it."""

    def __init__(self, texts: Iterable[str]) -> None:
        # (value, surrounding text, whether the source text is compact)
        self.occurrences: list[tuple[float, str, bool]] = []
        for text in texts:
            cleaned = _normalize(_clean(text))
            # A short tool result is wholly about the question (a unit conversion, a single
            # record), so its figures need no surrounding context. Context matters in long,
            # multi-record results, where a common number appears somewhere by coincidence.
            compact = len(cleaned) <= COMPACT_EVIDENCE_CHARACTERS
            for match in NUMBER.finditer(cleaned):
                window = cleaned[max(0, match.start() - WINDOW) : match.end() + WINDOW]
                self.occurrences.append((_value(match.group(1)), window, compact))

    def supports(self, token: str, words: set[str], topic: frozenset[str] = frozenset()) -> bool:
        value = float(token)
        places = _decimals(token)
        # Specific values (three or more significant digits, such as 2.30 or 46.6) rarely
        # match by coincidence; low-precision ones (0.5, 45, 60) often do, so they need more
        # corroborating context.
        significant = len(token.replace(".", "").lstrip("0"))
        needed = 1 if significant >= 3 or len(words) < 4 else 2
        for source, window, compact in self.occurrences:
            direct = abs(round(source, places) - value) < 1e-9
            if compact and direct:
                return True
            if not (direct or self._transformed(source, value, places)):
                continue
            # The question's own terms (usually the drug) count as at most one supporting
            # word: table rows rarely repeat the drug, but its presence alone must not be
            # enough to accept a coincidental number.
            matched = sum(1 for word in words if word in window)
            # Single-digit precision (0.5, 3) is too common for the topic alone to vouch for.
            if significant >= 2 and any(word in window for word in topic - words):
                matched += 1
            if matched >= needed:
                return True
        return False

    def restates(self, token: str) -> bool:
        """Whether a figure restates one of these values, as rounded, scaled, or complemented.

        Used for the answer's own calculations, where there is no coincidence to guard against
        and so no context is required.
        """
        value = float(token)
        places = _decimals(token)
        return any(
            abs(round(source, places) - value) < 1e-9 or self._transformed(source, value, places)
            for source, _, _ in self.occurrences
        )

    @staticmethod
    def _transformed(source: float, value: float, places: int) -> bool:
        """Unit scaling and bound/unbound complements. Always require matching context."""
        bases = [source]
        if 0 <= source <= 100:
            bases.append(100 - source)
        if 0 <= source <= 1:
            bases.append(1 - source)
        for base in bases:
            for scale in SCALES:
                if abs(round(base * scale, places) - value) < 1e-9:
                    return True
        return False


def ungrounded_numbers(
    answer: str, evidence_texts: Iterable[str], *, question: str = ""
) -> list[str]:
    """Figures in the answer that no retrieved text supports, in order of appearance."""
    body = SOURCE_SECTIONS.split(answer, maxsplit=1)[0]
    evidence = Evidence(evidence_texts)
    asked = {_value(token) for token in NUMBER.findall(question)}
    topic = frozenset(_context_words(question))
    flagged: list[str] = []

    lines = _clean(INVISIBLE.sub("", body)).splitlines()

    def builds_on_evidence(line: str) -> bool:
        words = _context_words(line)
        for match in NUMBER.finditer(line):
            token = match.group(1).replace(",", "")
            if _value(token) in asked or evidence.supports(token, words, topic):
                return True
        return False

    # Only calculations that start from grounded inputs yield derived values; "fu = 0.45"
    # with an invented 0.45 must not make a later "0.45" look supported.
    derived = Evidence(line for line in lines if "=" in line and builds_on_evidence(line))
    header: list[str] | None = None
    for line in lines:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        is_table = line.lstrip().startswith("|")
        if not is_table:
            header = None
        elif header is None:
            header = cells
            continue
        elif all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells if cell):
            continue  # the markdown separator row
        # Columns headed "pH", "temperature" and so on hold conditions, not data.
        condition_columns = {
            index for index, title in enumerate(header or []) if CONDITION_HEADER.search(title)
        }
        condition_spans = []
        if is_table and condition_columns:
            position = line.find("|") + 1
            for index, cell in enumerate(line.strip().strip("|").split("|")):
                width = len(cell) + 1
                if index in condition_columns:
                    condition_spans.append((position, position + width))
                position += width
        words = _context_words(line)
        candidates: list[str] = []
        grounded_in_line = False
        for match in NUMBER.finditer(line):
            token, start = match.group(1).replace(",", ""), match.start()
            in_condition_column = any(low <= start < high for low, high in condition_spans)
            if in_condition_column or CONDITION_AFTER.match(line[match.end() :]):
                continue
            if IDENTIFIER_BEFORE.search(line[:start]):
                continue
            value = float(token)
            if "." not in token and (value <= 10 or 1900 <= value <= 2100 or len(token) >= 6):
                continue  # counts, list markers, phases, classes, years, identifiers
            if value in asked or PH_VALUE.search(line[:start]):
                # Conditions the question set are not data, but they are legitimate inputs to
                # a calculation on the same line.
                grounded_in_line = True
                continue
            if evidence.supports(token, words, topic):
                grounded_in_line = True
            elif "=" not in line and derived.restates(token):
                pass  # restates the result of a calculation shown elsewhere in the answer
            else:
                candidates.append(token)
        # An explicit calculation built on grounded figures is derivation, not invention.
        if candidates and "=" in line and grounded_in_line:
            continue
        for token in candidates:
            if token not in flagged:
                flagged.append(token)
    return flagged


def unverified_note(figures: list[str]) -> str:
    if not figures:
        return ""
    shown = ", ".join(figures[:8]) + (" …" if len(figures) > 8 else "")
    return (
        f"\n\n> **Unverified figures:** {shown} - these do not appear in any source retrieved "
        "for this answer. Treat them as unconfirmed."
    )

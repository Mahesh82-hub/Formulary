"""Structured openFDA queries: the model describes what it wants, the application writes the
query.

Hand-written openFDA syntax was the root cause of a class of wrong answers. A model that
guessed ``manufacturer_name`` instead of ``openfda.manufacturer_name`` got "No matches found" -
the same response openFDA gives for a genuine absence - and told the user the data did not
exist. Here the model supplies JSON filters, every field is validated against openFDA's own
published field catalogue before any request is sent, unknown fields come back with
suggestions, and drug names are searched under both their international and US names.
"""

from __future__ import annotations

import json
import re
from difflib import get_close_matches
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.fda.names import search_names

CATALOGUE_PATH = Path(__file__).with_name("fields.json")
MatchMode = Literal["contains", "exact", "range", "exists"]
RANGE_BOUND = re.compile(r"^[0-9A-Za-z*\-.]+$")
ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

# Common guesses, mapped to the real field when it exists in the dataset being queried.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "manufacturer_name": ("openfda.manufacturer_name", "labeler_name"),
    "manufacturer": ("openfda.manufacturer_name", "labeler_name"),
    "labeler_name": ("openfda.manufacturer_name", "labeler_name"),
    "company": ("openfda.manufacturer_name", "labeler_name", "sponsor_name"),
    "sponsor": ("sponsor_name", "openfda.manufacturer_name"),
    "brand_name": ("openfda.brand_name", "brand_name"),
    "brand": ("openfda.brand_name", "brand_name"),
    "generic_name": ("openfda.generic_name", "generic_name"),
    "substance_name": ("openfda.substance_name",),
    "active_ingredients.name": ("active_ingredient", "active_ingredients.name"),
    "active_ingredients": ("active_ingredient", "active_ingredients.name"),
    "inactive_ingredients": ("inactive_ingredient",),
}


class FDAFilter(BaseModel):
    """One condition on one field. Filters are combined with AND unless combine is "any"."""

    field: str = Field(
        min_length=1,
        max_length=120,
        description="A field from the dataset's catalogue, e.g. openfda.brand_name.",
    )
    match: MatchMode = Field(
        default="contains",
        description=(
            "contains: phrase match (default, use for names and text); exact: whole-value "
            "match; range: start to end inclusive (dates as YYYY-MM-DD); exists: field present."
        ),
    )
    value: str | None = Field(default=None, max_length=300)
    start: str | None = Field(default=None, max_length=40)
    end: str | None = Field(default=None, max_length=40)


class CompiledQuery(BaseModel):
    search: str | None
    count: str | None
    sort: str | None
    notes: list[str] = Field(default_factory=list)


class FDAQueryRejection(BaseModel):
    """Returned instead of calling openFDA when a query would be malformed."""

    status: Literal["invalid_query"] = "invalid_query"
    dataset: str
    errors: list[str]
    hint: str = (
        "Nothing was sent to openFDA. Fix the fields above and call again, or use a focused "
        "tool such as get_drug_composition or get_fda_drug_labels. This is not evidence that "
        "the data does not exist."
    )


class QueryValidationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@lru_cache
def catalogue() -> dict[str, dict[str, dict[str, Any]]]:
    data: dict[str, dict[str, dict[str, Any]]] = json.loads(CATALOGUE_PATH.read_text())
    return data


def known_fields(dataset: str) -> dict[str, dict[str, Any]]:
    return catalogue().get(dataset, {})


def suggest_fields(dataset: str, field: str) -> list[str]:
    fields = known_fields(dataset)
    suggestions: list[str] = []
    for candidate in FIELD_ALIASES.get(field.casefold(), ()):
        if candidate in fields:
            suggestions.append(candidate)
    if f"openfda.{field}" in fields:
        suggestions.append(f"openfda.{field}")
    last = field.split(".")[-1]
    suggestions += [name for name in fields if name == last or name.endswith(f".{last}")]
    suggestions += get_close_matches(field, list(fields), n=3, cutoff=0.6)
    unique = list(dict.fromkeys(item for item in suggestions if item != field))
    return unique[:4]


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _bound(value: str) -> str:
    match = ISO_DATE.match(value)
    if match:
        return "".join(match.groups())
    if not RANGE_BOUND.match(value):
        raise ValueError(f"Range bound {value!r} may only contain letters, digits, '-', '.', '*'")
    return value


def compile_query(
    dataset: str,
    filters: list[FDAFilter],
    *,
    combine: Literal["all", "any"] = "all",
    count_field: str | None = None,
    sort_field: str | None = None,
    sort_order: Literal["asc", "desc"] = "desc",
) -> CompiledQuery:
    fields = known_fields(dataset)
    if not fields:
        raise QueryValidationError([f"No field catalogue is available for {dataset}."])
    errors: list[str] = []
    notes: list[str] = []

    def check(field: str, role: str) -> bool:
        if field in fields:
            return True
        suggestions = suggest_fields(dataset, field)
        hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
        errors.append(f"{role} {field!r} does not exist in {dataset}.{hint}")
        return False

    clauses: list[str] = []
    for item in filters:
        if not check(item.field, "Field"):
            continue
        try:
            clauses.append(_clause(item, fields[item.field], notes))
        except ValueError as error:
            errors.append(f"Filter on {item.field!r}: {error}")

    count = None
    if count_field and check(count_field, "Count field"):
        exact = fields[count_field].get("exact")
        count = f"{count_field}.exact" if exact else count_field
    sort = None
    if sort_field and check(sort_field, "Sort field"):
        sort = f"{sort_field}:{sort_order}"
    if errors:
        raise QueryValidationError(errors)

    joiner = " AND " if combine == "all" else " "
    search = joiner.join(f"({clause})" for clause in clauses) if clauses else None
    return CompiledQuery(search=search, count=count, sort=sort, notes=notes)


def _clause(item: FDAFilter, spec: dict[str, Any], notes: list[str]) -> str:
    if item.match == "exists":
        return f"_exists_:{item.field}"
    if item.match == "range":
        if not item.start or not item.end:
            raise ValueError("range needs both start and end")
        return f"{item.field}:[{_bound(item.start)} TO {_bound(item.end)}]"
    if not item.value or not item.value.strip():
        raise ValueError(f"{item.match} needs a value")

    names = search_names(item.value)
    if len(names) > 1:
        notes.append(
            f"{item.value!r} was also searched as {names[1]!r} (international and US names differ)."
        )
    if item.match == "exact":
        if not spec.get("exact"):
            notes.append(f"{item.field} has no exact form in openFDA; matched as a phrase.")
        else:
            # Exact values are case-sensitive and openFDA mostly stores them in capitals.
            values = list(dict.fromkeys([*names, *(name.upper() for name in names)]))
            return " ".join(f'{item.field}.exact:"{_escape(value)}"' for value in values)
    return " ".join(f'{item.field}:"{_escape(name)}"' for name in names)

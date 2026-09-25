"""Drug composition - active and inactive ingredients - from FDA labels.

Written after a real failure. Asked for the composition of paracetamol from a few top
manufacturers, the assistant asked an unnecessary question, then hand-wrote openFDA queries
against fields that do not exist, got zero results, and told the user that FDA holds no
inactive-ingredient data. In fact 3,066 of 3,474 acetaminophen labels list inactive
ingredients. This tool answers the question directly:

* international names are searched alongside US names (paracetamol and acetaminophen);
* with no manufacturer given, the top labelers are chosen from the data itself rather than by
  asking the user;
* all manufacturers are queried concurrently;
* OTC "Drug Facts" labels give active and inactive ingredients directly, and prescription
  labels fall back to their SPL product data, which lists every ingredient.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.fda.client import OpenFDAClient
from app.fda.models import FDAProvenance
from app.fda.names import search_names

DAILYMED_URL = "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={}"
NAME_FIELDS = ("openfda.generic_name", "openfda.brand_name", "openfda.substance_name")
# Repackagers relabel other companies' products; ranking them as manufacturers misleads.
REPACKAGER = re.compile(r"prepack|repack|relabel", re.IGNORECASE)
MAX_MANUFACTURERS = 8
TEXT_LIMIT = 1_500

CompositionSource = Literal["drug_facts", "spl_product_data", "not_listed"]


class ProductComposition(BaseModel):
    brand_name: str | None = None
    generic_name: str | None = None
    # True when the product combines the drug with other actives, e.g. a cold-and-flu product.
    is_combination: bool = False
    manufacturer: str | None = None
    product_type: str | None = None
    route: list[str] = Field(default_factory=list)
    active_ingredients: str | None = None
    inactive_ingredients: list[str] = Field(default_factory=list)
    inactive_ingredients_text: str | None = None
    product_data: str | None = None
    composition_source: CompositionSource
    effective_date: str | None = None
    set_id: str | None = None
    label_url: str | None = None


class ManufacturerComposition(BaseModel):
    manufacturer: str
    label_count: int | None = None
    products: list[ProductComposition]


class DrugCompositionResult(BaseModel):
    query: str
    searched_names: list[str]
    total_labels: int | None
    manufacturers: list[ManufacturerComposition]
    provenance: FDAProvenance | None
    caveats: list[str]


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def name_clause(names: list[str]) -> str:
    return " ".join(
        "(" + " ".join(f'{field}:"{_escape(name)}"' for field in NAME_FIELDS) + ")"
        for name in names
    )


def split_ingredients(text: str) -> list[str]:
    """Split a Drug Facts inactive-ingredient paragraph into individual ingredients.

    Multi-part packs (day and night doses) repeat the "Inactive ingredients" heading; each
    heading is treated as a separator so the two lists do not run together.
    """
    body = re.sub(r"inactive\s+ingredients?\s*[:\-]?", ",", text, flags=re.IGNORECASE)
    body = re.sub(r"\s+", " ", body).strip().strip(",").rstrip(".")
    items: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[,;]\s*", body):
        item = part.strip().strip(".").strip()
        if not item or len(item) > 120 or item.casefold() in seen:
            continue
        seen.add(item.casefold())
        items.append(item)
    return items[:60]


def _text(value: object) -> str | None:
    if isinstance(value, list):
        value = " ".join(item for item in value if isinstance(item, str))
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = " ".join(value.split())
    return cleaned if len(cleaned) <= TEXT_LIMIT else cleaned[: TEXT_LIMIT - 1] + "…"


def _first(value: object) -> str | None:
    if isinstance(value, list):
        return next((item for item in value if isinstance(item, str) and item.strip()), None)
    return value if isinstance(value, str) else None


def product_composition(
    record: dict[str, Any], names: list[str] | None = None
) -> ProductComposition:
    raw_openfda = record.get("openfda")
    openfda: dict[str, Any] = raw_openfda if isinstance(raw_openfda, dict) else {}
    active = _text(record.get("active_ingredient"))
    inactive = _text(record.get("inactive_ingredient"))
    product_data = _text(record.get("spl_product_data_elements"))
    source: CompositionSource
    if active or inactive:
        source = "drug_facts"
    elif product_data:
        source = "spl_product_data"
    else:
        source = "not_listed"
    set_id = record.get("set_id") if isinstance(record.get("set_id"), str) else None
    route = openfda.get("route")
    generic = _first(openfda.get("generic_name"))
    wanted = {name.casefold() for name in names or []}
    return ProductComposition(
        brand_name=_first(openfda.get("brand_name")),
        generic_name=generic,
        is_combination=bool(wanted) and (generic or "").casefold() not in wanted,
        manufacturer=_first(openfda.get("manufacturer_name")),
        product_type=_first(openfda.get("product_type")),
        route=[item for item in route if isinstance(item, str)] if isinstance(route, list) else [],
        active_ingredients=active,
        inactive_ingredients=split_ingredients(inactive) if inactive else [],
        inactive_ingredients_text=inactive,
        product_data=product_data if source == "spl_product_data" else None,
        composition_source=source,
        effective_date=record.get("effective_time")
        if isinstance(record.get("effective_time"), str)
        else None,
        set_id=set_id,
        label_url=DAILYMED_URL.format(set_id) if set_id else None,
    )


async def drug_composition(
    fda: OpenFDAClient,
    drug: str,
    *,
    manufacturers: list[str] | None = None,
    max_manufacturers: int = 5,
    products_per_manufacturer: int = 2,
) -> DrugCompositionResult:
    names = search_names(drug)
    if not names:
        raise ValueError("A drug name is required")
    clause = name_clause(names)
    caveats: list[str] = []
    if len(names) > 1:
        caveats.append(
            f"Searched {' and '.join(repr(name) for name in names)}: US labels use United "
            "States Adopted Names, which differ from some international names."
        )

    bounded = min(max(1, max_manufacturers), MAX_MANUFACTURERS)
    total: int | None = None
    provenance: FDAProvenance | None = None
    chosen: list[tuple[str, int | None]]
    if manufacturers:
        requested = [name.strip() for name in manufacturers if name.strip()]
        chosen = [(name, None) for name in requested[:MAX_MANUFACTURERS]]
    else:
        counted, overall = await asyncio.gather(
            fda.query(
                "drug/label", search=clause, count="openfda.manufacturer_name.exact", limit=25
            ),
            fda.query("drug/label", search=clause, limit=1),
        )
        provenance = counted.provenance
        total = overall.total
        ranked: list[tuple[str, int | None]] = [
            (str(row["term"]), int(row["count"]))
            for row in counted.results
            if isinstance(row.get("term"), str)
            and isinstance(row.get("count"), int)
            and not REPACKAGER.search(row["term"])
        ]
        chosen = ranked[:bounded]
        if ranked:
            caveats.append(
                "Manufacturers were chosen by the number of current FDA labels they hold, which "
                "is not market share. Retailers such as store brands can rank highly because "
                "they are the labeler of record for private-label products; repackagers were "
                "excluded."
            )

    single = " ".join(f'openfda.generic_name.exact:"{_escape(name.upper())}"' for name in names)

    async def fetch(manufacturer: str, label_count: int | None) -> ManufacturerComposition:
        by = f'openfda.manufacturer_name:"{_escape(manufacturer)}"'
        # A composition question is about the drug itself, so products containing only this
        # active come first; combination products fill any remaining slots.
        single_page, broad_page = await asyncio.gather(
            fda.query(
                "drug/label",
                search=f"({single}) AND {by}",
                sort="effective_time:desc",
                limit=min(10, products_per_manufacturer * 3),
            ),
            fda.query(
                "drug/label",
                search=f"({clause}) AND {by}",
                sort="effective_time:desc",
                limit=min(10, products_per_manufacturer * 3),
            ),
        )
        products: list[ProductComposition] = []
        seen: set[str] = set()
        for record in [*single_page.results, *broad_page.results]:
            product = product_composition(record, names)
            key = (product.brand_name or product.set_id or "").casefold()
            if key in seen:
                continue
            seen.add(key)
            products.append(product)
            if len(products) >= products_per_manufacturer:
                break
        return ManufacturerComposition(
            manufacturer=manufacturer, label_count=label_count, products=products
        )

    results = await asyncio.gather(*(fetch(name, count) for name, count in chosen))
    found = [item for item in results if item.products]
    missing = [item.manufacturer for item in results if not item.products]
    if missing:
        caveats.append(
            "No current FDA label matched for: "
            + ", ".join(missing)
            + ". Check the spelling of the manufacturer; the product may be marketed under a "
            "different company name or only outside the US."
        )
    if any(product.is_combination for item in found for product in item.products):
        caveats.append(
            "Some listed products combine this drug with other actives (is_combination); "
            "single-ingredient products are listed first where the manufacturer has them."
        )
    if any(
        product.composition_source == "spl_product_data"
        for item in found
        for product in item.products
    ):
        caveats.append(
            "Prescription labels do not have a separate inactive-ingredients section; their "
            "product_data field lists every ingredient, active and inactive, from the SPL."
        )
    return DrugCompositionResult(
        query=drug,
        searched_names=names,
        total_labels=total,
        manufacturers=found,
        provenance=provenance,
        caveats=caveats,
    )

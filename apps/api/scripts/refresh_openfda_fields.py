"""Regenerate app/fda/fields.json from openFDA's published field definitions.

    python -m scripts.refresh_openfda_fields

openFDA publishes one YAML file per dataset (drug/label -> druglabel.yaml). The flattened
catalogue lets the structured query tool validate every field before a request is sent, so a
misspelled or invented field is rejected with suggestions instead of silently matching nothing.
Re-run when openFDA announces schema changes; the diff shows exactly which fields moved.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import yaml

from app.fda.models import OPENFDA_DATASETS

FIELDS_URL = "https://open.fda.gov/fields/{}.yaml"
OUTPUT = Path(__file__).resolve().parents[1] / "app" / "fda" / "fields.json"
# Most files are named by dropping the slash (drug/label -> druglabel); these are not.
FILE_NAMES = {
    "drug/drugsfda": "drugsfda",
    "device/510k": "deviceclearance",
    "device/classification": "deviceclass",
    "device/registrationlisting": "devicereglist",
}


def flatten(properties: dict[str, Any], prefix: str = "") -> dict[str, dict[str, Any]]:
    fields: dict[str, dict[str, Any]] = {}
    for name, spec in (properties or {}).items():
        if not isinstance(spec, dict):
            continue
        path = f"{prefix}{name}"
        kind = spec.get("type")
        raw_items = spec.get("items")
        items: dict[str, Any] = raw_items if isinstance(raw_items, dict) else {}
        nested = spec.get("properties") or items.get("properties")
        if isinstance(nested, dict):
            fields.update(flatten(nested, f"{path}."))
            continue
        leaf = items or spec
        description = " ".join(str(leaf.get("description") or "").split())
        fields[path] = {
            "type": leaf.get("type") or kind or "string",
            "exact": bool(leaf.get("is_exact")),
            "description": description[:160],
        }
    return fields


async def main() -> None:
    catalogue: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient(timeout=60) as client:
        for dataset in sorted(OPENFDA_DATASETS):
            name = FILE_NAMES.get(dataset, dataset.replace("/", ""))
            response = await client.get(FIELDS_URL.format(name))
            if response.status_code != 200:
                print(f"skip {dataset}: HTTP {response.status_code}")
                continue
            document = yaml.safe_load(response.text) or {}
            fields = flatten(document.get("properties") or {})
            catalogue[dataset] = fields
            print(f"{dataset:<32} {len(fields):>4} fields")
    OUTPUT.write_text(json.dumps(catalogue, indent=1, sort_keys=True) + "\n")
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    asyncio.run(main())

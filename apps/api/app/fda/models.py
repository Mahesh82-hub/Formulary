from typing import Any, Literal, get_args

from pydantic import BaseModel, Field

from app.sources.models import SourceProvenance

OpenFDADataset = Literal[
    "animalandveterinary/event",
    "cosmetic/event",
    "device/510k",
    "device/classification",
    "device/covid19serology",
    "device/enforcement",
    "device/event",
    "device/pma",
    "device/recall",
    "device/registrationlisting",
    "device/udi",
    "drug/drugsfda",
    "drug/enforcement",
    "drug/event",
    "drug/label",
    "drug/ndc",
    "drug/orangebook",
    "drug/shortages",
    "food/enforcement",
    "food/event",
    "other/historicaldocument",
    "other/nsde",
    "other/substance",
    "other/unii",
    "tobacco/problem",
    "tobacco/researchdigitalads",
    "tobacco/researchpreventionads",
    "tobacco/researchsmokefree",
    "transparency/crl",
]

OPENFDA_DATASETS: frozenset[str] = frozenset(get_args(OpenFDADataset))

# openFDA rejects paging beyond this offset. Declared once so the client guard and the
# MCP tool clamp cannot drift apart.
OPENFDA_MAX_SKIP = 25_000
# openFDA's largest accepted page size.
OPENFDA_MAX_PAGE_SIZE = 1_000


class OpenFDAQuery(BaseModel):
    dataset: str
    search: str | None = None
    count: str | None = None
    sort: str | None = None
    limit: int = Field(ge=1)
    skip: int = Field(ge=0)


class FDAProvenance(SourceProvenance):
    """openFDA provenance.

    Inherits the shared shape so a federated answer can cite openFDA and any other source
    through one interface; only the default source name is specialised.
    """

    source: str = "openFDA"


class OpenFDAResult(BaseModel):
    query: OpenFDAQuery
    total: int | None = None
    returned: int
    results: list[dict[str, Any]]
    provenance: FDAProvenance
    caveats: list[str] = Field(default_factory=list)
    # "invalid_query" means the request was rejected before being sent; see errors. It is
    # never evidence that data is absent.
    status: Literal["ok", "invalid_query"] = "ok"
    errors: list[str] = Field(default_factory=list)

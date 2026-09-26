from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field

from app.fda.catalogue import datasets
from app.sources.models import SourceProvenance

OPENFDA_DATASETS: frozenset[str] = datasets()


def _known_dataset(value: str) -> str:
    if value not in OPENFDA_DATASETS:
        raise ValueError(f"Unknown openFDA dataset {value!r}")
    return value


# Derived from openFDA's published catalogue rather than typed out by hand. The enum reaches
# the model through the tool schema, so it cannot invent a dataset; the validator enforces it,
# which matters because the dataset name becomes part of the request URL.
OpenFDADataset = Annotated[
    str,
    AfterValidator(_known_dataset),
    Field(json_schema_extra={"enum": sorted(OPENFDA_DATASETS)}),
]

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

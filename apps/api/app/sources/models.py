"""Provenance shared by every upstream evidence source.

Answers must be attributable: a user needs to see which source a claim came from, when it was
retrieved, and under what licence and disclaimer. Every source therefore returns the same
provenance shape so the chat layer can render citations uniformly.
"""

from datetime import datetime

from pydantic import BaseModel


class SourceProvenance(BaseModel):
    """Where a record came from and what constraints the source places on it."""

    source: str
    api_url: str
    retrieved_at: datetime
    dataset_last_updated: str | None = None
    disclaimer: str | None = None
    license_url: str | None = None
    terms_url: str | None = None
    next_page_url: str | None = None

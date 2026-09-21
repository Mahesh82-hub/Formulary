"""FDA data access boundary."""

from app.fda.client import OpenFDAClient, get_openfda_client

__all__ = ["OpenFDAClient", "get_openfda_client"]

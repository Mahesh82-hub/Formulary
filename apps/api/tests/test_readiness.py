import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.mark.integration
def test_readiness_reports_postgres_and_pgvector() -> None:
    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["postgres_version"].startswith("18.")
    assert body["pgvector_version"]

import os

os.environ["LLM_MODE"] = "demo"

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


get_settings.cache_clear()
client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_invalid_request_is_400():
    response = client.post("/optimize-energy", json={"scenario_id": "x"})
    assert response.status_code == 400

import re

import pytest
from fastapi.testclient import TestClient

from deployra_panel.app import create_app
from deployra_panel.config import Config


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOYRA_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("DEPLOYRA_WG", "off")
    monkeypatch.setenv("DEPLOYRA_MONITORING", "off")
    return Config()


@pytest.fixture
def client(config):
    app = create_app(config)
    with TestClient(app, follow_redirects=False) as c:
        yield c


def csrf_of(client: TestClient) -> str:
    page = client.get("/settings").text
    return re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)


@pytest.fixture
def admin(client):
    """Konfiguruje panel i zwraca klienta zalogowanego jako administrator (z nagłówkiem CSRF)."""
    response = client.post(
        "/setup",
        data={"name": "Ada", "surname": "Lovelace", "email": "admin@example.com", "password": "bardzo-tajne", "password_repeat": "bardzo-tajne",
              "base_domain": "example.com", "panel_domain": "deploy.example.com"},
        headers={"HX-Request": "true"},
    )  # fmt: skip
    assert response.status_code == 204 and response.headers["HX-Redirect"] == "/"
    client.headers.update({"X-CSRF-Token": csrf_of(client), "HX-Request": "true"})
    return client

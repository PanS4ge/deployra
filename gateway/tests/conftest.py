import pytest

from deployra_gateway.config import Config
from deployra_gateway.context import Context


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOYRA_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("DEPLOYRA_NGINX_DIR", str(tmp_path / "nginx"))
    monkeypatch.setenv("DEPLOYRA_CERTS_DIR", str(tmp_path / "certs"))
    monkeypatch.setenv("DEPLOYRA_ACME_DIR", str(tmp_path / "acme"))
    monkeypatch.setenv("DEPLOYRA_PUBLIC_HOST", "vps.example.com")
    monkeypatch.setenv("DEPLOYRA_WG", "off")
    monkeypatch.setenv("DEPLOYRA_NGINX_RELOAD", "off")
    monkeypatch.setenv("DEPLOYRA_IPV6", "on")
    return Context(Config())

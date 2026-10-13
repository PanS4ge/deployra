import asyncio
import time
import httpx
import pytest

from deployra_common.schemas import AppSpec
from deployra_panel.permissions import Permission
from deployra_panel.services import alerts, deploy, uptime
from deployra_panel.i18n import t


class FakeNode:
    def __init__(self, containers=None):
        self.containers = containers or []
        self.calls = []

    async def get(self, path, **_):
        self.calls.append(("GET", path))
        return self.containers

    async def post(self, path, json=None, **_):
        self.calls.append(("POST", path, json))
        return {"ok": True}


class FailingGateway:
    def __init__(self):
        self.calls = []

    async def post(self, path, json=None, **_):
        self.calls.append(json)
        # Błąd bramki z powodu niedostępnej / wygasłej domeny
        return {"status": None, "ok": False, "error": "NXDOMAIN"}


def test_uptime_fallback_when_domain_fails(admin, monkeypatch):
    """Protokół 2: Gdy domena wygasła/nie odpowiada, ale kontener działa -> ONLINE (ok=1)."""
    panel = admin.app.state.panel
    panel.db.insert("nodes", name="node1", transport="direct", api_url="http://127.0.0.1:1", status="paired")
    
    # Kontener aplikacji w stanie running
    node = FakeNode([
        {"id": "cid1", "name": "app-wygasla", "app": "wygasla", "state": "running", "status": "Up 5 hours"}
    ])
    gateway = FailingGateway()
    monkeypatch.setattr(panel, "node_client", lambda n: node)
    monkeypatch.setattr(panel, "gateway_client", lambda: gateway)

    source = {"kind": "image", "image": "nginx:alpine"}
    spec = AppSpec.model_validate({"slug": "wygasla", "source": source, "container_port": 80, "domains": ["wygasla.pl"]})
    app_id = deploy.create_app_row(panel, "wygasla", 1, spec, {})
    panel.db.update("apps", app_id, status="running", host_port=30080)
    monitor = uptime.monitor_for(panel, "app", app_id)

    checker = uptime.Checker(panel)
    ok, latency, msg = asyncio.run(checker.check(monitor))

    # Powinien zadziałać fallback do kontenera
    assert ok is True
    assert latency is None
    assert t("uptime.msg.container_fallback") in msg

    # Rejestracja wyniku w bazie danych: ok=1, brak spadku licznika uptime
    uptime.record(panel, monitor, ok, latency, msg)
    assert panel.db.get("monitors", monitor["id"])["status"] == "up"

    last_check = panel.db.fetchone("SELECT * FROM monitor_checks WHERE monitor_id = ? ORDER BY ts DESC", [monitor["id"]])
    assert last_check["ok"] == 1


def test_uptime_fallback_fails_when_container_stopped(admin, monkeypatch):
    """Protokół 2: Gdy domena nie odpowiada i kontener też nie działa -> status False."""
    panel = admin.app.state.panel
    panel.db.insert("nodes", name="node2", transport="direct", api_url="http://127.0.0.1:1", status="paired")

    # Kontener wyłączony
    node = FakeNode([
        {"id": "cid2", "name": "app-padla", "app": "padla", "state": "exited", "status": "Exited (1)"}
    ])
    gateway = FailingGateway()
    monkeypatch.setattr(panel, "node_client", lambda n: node)
    monkeypatch.setattr(panel, "gateway_client", lambda: gateway)

    source = {"kind": "image", "image": "nginx:alpine"}
    spec = AppSpec.model_validate({"slug": "padla", "source": source, "container_port": 80, "domains": ["padla.pl"]})
    app_id = deploy.create_app_row(panel, "padla", 1, spec, {})
    panel.db.update("apps", app_id, status="running", host_port=30081)
    monitor = uptime.monitor_for(panel, "app", app_id)

    checker = uptime.Checker(panel)
    ok, latency, msg = asyncio.run(checker.check(monitor))

    assert ok is False


def test_uptime_fallback_on_connect_error(admin, monkeypatch):
    """Protokół 2: Gdy brak gatewaya i httpx rzuca ConnectError na domenie -> fallback kontenera."""
    panel = admin.app.state.panel
    panel.db.insert("nodes", name="node3", transport="direct", api_url="http://127.0.0.1:1", status="paired")

    node = FakeNode([
        {"id": "cid3", "name": "app-dnserror", "app": "dnserror", "state": "running", "status": "Up 1 hour"}
    ])
    monkeypatch.setattr(panel, "node_client", lambda n: node)
    monkeypatch.setattr(panel, "gateway_client", lambda: None)

    source = {"kind": "image", "image": "nginx:alpine"}
    spec = AppSpec.model_validate({"slug": "dnserror", "source": source, "container_port": 80, "domains": ["dnserror.pl"]})
    app_id = deploy.create_app_row(panel, "dnserror", 1, spec, {})
    panel.db.update("apps", app_id, status="running", host_port=30082)
    monitor = uptime.monitor_for(panel, "app", app_id)

    checker = uptime.Checker(panel)

    async def mock_get(*args, **kwargs):
        raise httpx.ConnectError("[Errno -2] Name or service not known")

    monkeypatch.setattr(checker._http, "get", mock_get)

    ok, latency, msg = asyncio.run(checker.check(monitor))
    assert ok is True
    assert latency is None
    assert t("uptime.msg.container_fallback") in msg


def test_alert_ignoring_flow(admin, monkeypatch):
    """Protokół 3: Wyciszanie/ignorowanie alertu zapobiega wysyłaniu powiadomień."""
    panel = admin.app.state.panel
    now = time.time()
    alert_id = panel.db.insert(
        "alerts", rule="node_cpu", target_kind="node", target_id=1, target_name="serwer-1",
        status="firing", message="CPU 99%", value=99.0, started_at=now
    )

    dispatched = []
    async def mock_dispatch(p, ch, msg):
        dispatched.append(msg)
    monkeypatch.setattr(alerts, "dispatch", mock_dispatch)

    # Ignoruj alert przez POST /alerts/{id}/ignore
    resp = admin.post(f"/alerts/{alert_id}/ignore")
    assert resp.status_code in (200, 204, 303)

    # Sprawdź stan w bazie danych
    row = panel.db.get("alerts", alert_id)
    assert row["status"] == "ignored"
    assert row.get("ignored_at") is not None

    # Ponowne zgłoszenie tego samego alertu nie powinno wysłać powiadomienia
    new_id = asyncio.run(alerts.fire(panel, "node_cpu", "node", 1, "serwer-1", 99.0, "CPU 99%"))
    assert new_id == alert_id
    assert len(dispatched) == 0

    # monitor_change dla zignorowanego monitora nie wysyła powiadomienia
    monitor_alert_id = panel.db.insert(
        "alerts", rule="monitor_down", target_kind="monitor", target_id=42, target_name="Sklep",
        status="ignored", message="down", value=None, started_at=now
    )
    asyncio.run(alerts.monitor_change(panel, {"id": 42, "name": "Sklep", "kind": "app"}, "down", "down"))
    assert len(dispatched) == 0

    # Zamknięcie zignorowanego alertu
    resp_close = admin.post(f"/alerts/{alert_id}/close")
    assert resp_close.status_code in (200, 204, 303)
    row_closed = panel.db.get("alerts", alert_id)
    assert row_closed["status"] == "resolved"


def test_app_container_controls(admin, monkeypatch):
    """Protokół 1: Sterowanie kontenerem aplikacji przez POST /apps/{id}/action/{action}."""
    panel = admin.app.state.panel
    panel.db.insert("nodes", name="node_ctrl", transport="direct", api_url="http://127.0.0.1:1", status="paired")

    node = FakeNode([
        {"id": "c_ctrl_1", "name": "app-ctrl", "app": "ctrl", "state": "running", "status": "Up 1 hour"}
    ])
    monkeypatch.setattr(panel, "node_client", lambda n: node)

    source = {"kind": "image", "image": "nginx:alpine"}
    spec = AppSpec.model_validate({"slug": "ctrl", "source": source, "container_port": 80, "domains": []})
    app_id = deploy.create_app_row(panel, "ctrl", 1, spec, {})
    panel.db.update("apps", app_id, status="running")

    # Restart
    r1 = admin.post(f"/apps/{app_id}/action/restart")
    assert r1.status_code == 200
    assert any("restart" in str(c) for c in node.calls)

    # Stop
    r2 = admin.post(f"/apps/{app_id}/action/stop")
    assert r2.status_code == 200
    assert any("stop" in str(c) for c in node.calls)
    assert panel.db.get("apps", app_id)["status"] == "stopped"

    # Start
    r3 = admin.post(f"/apps/{app_id}/action/start")
    assert r3.status_code == 200
    assert any("start" in str(c) for c in node.calls)
    assert panel.db.get("apps", app_id)["status"] == "running"

    # Kill
    r4 = admin.post(f"/apps/{app_id}/action/kill")
    assert r4.status_code == 200
    assert any("kill" in str(c) for c in node.calls)
    assert panel.db.get("apps", app_id)["status"] == "stopped"

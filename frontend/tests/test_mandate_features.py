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


def test_stopped_app_does_not_alert_and_resolves_active_alert(admin, monkeypatch):
    """Zatrzymana przez użytkownika aplikacja nie generuje alertów i rozwiązuje aktywny alert."""
    panel = admin.app.state.panel
    panel.db.insert("nodes", name="node_stopped", transport="direct", api_url="http://127.0.0.1:1", status="paired")

    source = {"kind": "image", "image": "bot:latest"}
    spec = AppSpec.model_validate({"slug": "mybot", "source": source, "container_port": 80, "domains": []})
    app_id = deploy.create_app_row(panel, "mybot", 1, spec, {})
    panel.db.update("apps", app_id, status="stopped")

    monitor = uptime.monitor_for(panel, "app", app_id)
    # Symulujemy stary aktywny alert w bazie
    alert_id = panel.db.insert(
        "alerts", rule="monitor_down", target_kind="monitor", target_id=monitor["id"],
        target_name=monitor["name"], status="firing", message="nie działa", started_at=time.time()
    )

    checker = uptime.Checker(panel)
    ok, latency, msg = asyncio.run(checker.check(monitor))

    # Stan ok is None (pomijany / brak awarii)
    assert ok is None
    assert t("uptime.msg.stopped") in msg

    # Monitor w stanie paused, a stary alert rozwiązany
    assert panel.db.get("monitors", monitor["id"])["status"] == "paused"
    assert panel.db.get("alerts", alert_id)["status"] == "resolved"


def test_undeployed_app_does_not_alert_and_resolves_alert(admin, monkeypatch):
    """Aplikacja bez przypisanego węzła (niewdrożona) nie raportuje awarii jako alert."""
    panel = admin.app.state.panel
    source = {"kind": "image", "image": "draft:latest"}
    spec = AppSpec.model_validate({"slug": "draftapp", "source": source, "container_port": 80, "domains": []})
    app_id = deploy.create_app_row(panel, "draftapp", 999, spec, {})
    panel.db.update("apps", app_id, status="running")

    monitor = uptime.monitor_for(panel, "app", app_id)
    alert_id = panel.db.insert(
        "alerts", rule="monitor_down", target_kind="monitor", target_id=monitor["id"],
        target_name=monitor["name"], status="firing", message="aplikacja nie jest wdrożona", started_at=time.time()
    )

    checker = uptime.Checker(panel)
    ok, latency, msg = asyncio.run(checker.check(monitor))

    assert ok is None
    assert t("uptime.msg.not_deployed") in msg
    assert panel.db.get("monitors", monitor["id"])["status"] == "paused"
    assert panel.db.get("alerts", alert_id)["status"] == "resolved"


def test_cleanly_exited_container_treated_as_stopped_user_choice(admin, monkeypatch):
    """Kontener zakończony kodem 0 (np. docker stop) traktowany jako wybór użytkownika (stopped/paused)."""
    panel = admin.app.state.panel
    panel.db.insert("nodes", name="node_clean", transport="direct", api_url="http://127.0.0.1:1", status="paired")

    node = FakeNode([
        {"id": "c_clean", "name": "app-cleanbot", "app": "cleanbot", "state": "exited", "status": "Exited (0) 10 minutes ago"}
    ])
    monkeypatch.setattr(panel, "node_client", lambda n: node)

    source = {"kind": "image", "image": "cleanbot:latest"}
    spec = AppSpec.model_validate({"slug": "cleanbot", "source": source, "container_port": 80, "domains": []})
    app_id = deploy.create_app_row(panel, "cleanbot", 1, spec, {})
    panel.db.update("apps", app_id, status="running")

    monitor = uptime.monitor_for(panel, "app", app_id)
    checker = uptime.Checker(panel)
    ok, latency, msg = asyncio.run(checker.check(monitor))

    assert ok is None
    assert panel.db.get("apps", app_id)["status"] == "stopped"
    assert panel.db.get("monitors", monitor["id"])["status"] == "paused"


def test_resolve_inactive_app_alerts_cleans_dashboard(admin):
    """Funkcja resolve_inactive_app_alerts automatycznie czyści dashboard z alertów dla zatrzymanych aplikacji."""
    panel = admin.app.state.panel
    panel.db.insert("nodes", name="node_dash", transport="direct", api_url="http://127.0.0.1:1", status="paired")

    source = {"kind": "image", "image": "test:latest"}
    spec = AppSpec.model_validate({"slug": "stoppedapp", "source": source, "container_port": 80, "domains": []})
    app_id = deploy.create_app_row(panel, "stoppedapp", 1, spec, {})
    panel.db.update("apps", app_id, status="stopped")

    monitor = uptime.monitor_for(panel, "app", app_id)
    panel.db.insert(
        "alerts", rule="monitor_down", target_kind="monitor", target_id=monitor["id"],
        target_name=monitor["name"], status="firing", message="nie działa", started_at=time.time()
    )

    # Przed wywołaniem - alert firing
    firing_before = panel.db.fetchall("SELECT * FROM alerts WHERE status = 'firing'")
    assert len(firing_before) >= 1

    # alerts.firing() automatycznie wywołuje resolve_inactive_app_alerts
    active = alerts.firing(panel)
    assert not any(a["target_id"] == monitor["id"] for a in active)

"""Brakujące alerty: certyfikat wygasa, bramka nie odpowiada, zadanie cykliczne / kopia panelu nie wyszły."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from deployra_panel.services import alerts, watch


@pytest.fixture
def events(monkeypatch):
    seen = []

    async def capture(panel, rule_id, message):
        seen.append((rule_id, message.title))
        return True

    monkeypatch.setattr(alerts, "event", capture)
    return seen


def _firing(panel, rule):
    return panel.db.fetchall("SELECT * FROM alerts WHERE rule = ? AND status = 'firing'", [rule])


def test_certificate_expiring_fires_once_and_resolves(admin, events):
    panel = admin.app.state.panel
    soon = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    cert = panel.db.insert("certificates", name="x", domains=["app.example.com"], challenge="http", status="valid", expires_at=soon)
    asyncio.run(watch.certificates(panel))
    asyncio.run(watch.certificates(panel))
    assert len(_firing(panel, "cert_expiring")) == 1 and [e[0] for e in events] == ["cert_expiring"]  # raz, nie co godzinę
    panel.db.update("certificates", cert, expires_at=(datetime.now(timezone.utc) + timedelta(days=80)).isoformat())
    asyncio.run(watch.certificates(panel))
    assert _firing(panel, "cert_expiring") == [] and events[-1][0] == "cert_expiring"  # wiadomość „odnowiony”


def test_gateway_offline_after_grace(admin, events, monkeypatch):
    panel = admin.app.state.panel
    gw = panel.db.insert("gateway", name="oracle", role="edge", status="paired", secret=panel.sealer.seal(b"e" * 32), api_url="http://10.66.0.7:2751")

    class Down:
        async def get(self, path, **_):
            raise OSError("brak trasy")

    monkeypatch.setattr(panel, "client_for_gateway", lambda g: Down())
    asyncio.run(watch.gateways(panel))
    assert _firing(panel, "gateway_offline") == []  # chwilowa przerwa — jeszcze bez alertu
    watch._DOWN_SINCE[gw] -= watch.GATEWAY_GRACE + 1
    asyncio.run(watch.gateways(panel))
    assert len(_firing(panel, "gateway_offline")) == 1

    class Up:
        async def get(self, path, **_):
            return {"peers": []}

    monkeypatch.setattr(panel, "client_for_gateway", lambda g: Up())
    asyncio.run(watch.gateways(panel))
    assert _firing(panel, "gateway_offline") == []


def test_failed_task_and_auto_backup_alert(admin, events, monkeypatch):
    from deployra_panel.services import backup_storage

    panel = admin.app.state.panel
    asyncio.run(watch.task_failed(panel, {"id": 1, "app_id": 0, "name": "kopia"}, "failed", "exit 1"))
    assert events[-1][0] == "task_failed"
    panel.db.set_setting("backup_config", {"target": "local", "auto_hours": 24})
    calls = []
    monkeypatch.setattr(backup_storage, "run_backup", lambda p: calls.append(1) or {"ok": False, "error": "dysk pełny"})
    asyncio.run(watch.panel_backup(panel))
    asyncio.run(watch.panel_backup(panel))
    assert calls == [1] and events[-1][0] == "panel_backup_failed"  # drugi raz za wcześnie — bez kopii

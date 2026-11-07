"""Dziennik zdarzeń: logowania, zmiany (bez treści formularzy), strona z filtrami, CSV, alert o nowym adresie."""

from __future__ import annotations

from fastapi.testclient import TestClient

from deployra_common.passwords import hash_password


def _rows(panel, action=None):
    rows = panel.db.fetchall("SELECT * FROM audit_log ORDER BY id")
    return [r for r in rows if action is None or r["action"] == action]


def test_logins_and_changes_are_recorded_without_form_bodies(admin):
    panel = admin.app.state.panel
    bad = TestClient(admin.app, follow_redirects=False)
    bad.post("/login", data={"email": "admin@example.com", "password": "zle-haslo-xyz"})
    failed = _rows(panel, "login")[-1]
    assert failed["ok"] == 0 and failed["user_email"] == "admin@example.com"
    admin.post("/settings/users/save", data={"email": "nowy@example.com", "password": "Sekret-123456"})
    change = next(r for r in _rows(panel, "request") if r["path"] == "/settings/users/save")
    assert change["method"] == "POST" and change["user_email"] == "admin@example.com" and change["status"] in (204, 303)
    assert "Sekret-123456" not in str(_rows(panel))  # hasło z formularza nie trafia do dziennika


def test_audit_page_filters_and_csv(admin):
    panel = admin.app.state.panel
    admin.post("/settings/mcp/tokens", data={"name": "agent", "days": "30"})
    page = admin.get("/settings/audit", params={"q": "/settings/mcp"}, headers={"HX-Request": ""}).text
    assert "/settings/mcp/tokens" in page
    csv = admin.get("/settings/audit.csv", params={"days": "1"})
    assert csv.headers["content-type"].startswith("text/csv") and "/settings/mcp/tokens" in csv.text
    panel.db.insert("users", email="viewer@example.com", password_hash=hash_password("viewer-pass-1"), permissions="", created_at="x", undeletable=0)
    viewer = TestClient(admin.app, follow_redirects=False)
    viewer.post("/login", data={"email": "viewer@example.com", "password": "viewer-pass-1"})
    assert viewer.get("/settings/audit").status_code == 403


def test_new_ip_alert_only_after_known_history(admin, monkeypatch):
    from deployra_panel.services import alerts

    panel = admin.app.state.panel
    fired = []

    async def capture(panel_, rule, message, *a, **k):
        fired.append(rule)

    monkeypatch.setattr(alerts, "event", capture)
    panel.db.insert("users", email="ola@example.com", password_hash=hash_password("ola-haslo-12"), permissions="", created_at="x", undeletable=0)
    first = TestClient(admin.app, follow_redirects=False, client=("198.51.100.1", 5000))
    first.post("/login", data={"email": "ola@example.com", "password": "ola-haslo-12"})
    assert fired == []  # pierwsze logowanie w historii — nie ma z czym porównać
    again = TestClient(admin.app, follow_redirects=False, client=("198.51.100.1", 5001))
    again.post("/login", data={"email": "ola@example.com", "password": "ola-haslo-12"})
    assert fired == []  # znany adres
    other = TestClient(admin.app, follow_redirects=False, client=("203.0.113.77", 5002))
    other.post("/login", data={"email": "ola@example.com", "password": "ola-haslo-12"})
    assert fired == ["login_new_ip"]

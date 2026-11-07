"""2FA kont panelu: włączenie, logowanie dwuetapowe, kody zapasowe, wymuszenie, reset przez administratora, sesje."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from deployra_common import totp
from deployra_common.passwords import hash_password


def _enable(admin) -> tuple[str, list[str]]:
    setup = admin.post("/account/2fa/start").text
    secret = re.search(r"secret=([A-Z2-7]+)", setup).group(1)
    done = admin.post("/account/2fa/enable", data={"code": totp.code_at(secret, totp.now_step())})
    assert done.status_code == 200, done.text
    codes = re.findall(r"[A-Z2-9]{4}-[A-Z2-9]{4}", done.text)
    return secret, sorted(set(codes))


def _fresh(admin) -> TestClient:
    return TestClient(admin.app, follow_redirects=False, headers={"HX-Request": "true"})


def test_enable_and_two_step_login(admin):
    panel = admin.app.state.panel
    secret, codes = _enable(admin)
    me = panel.db.fetchone("SELECT * FROM users WHERE email = 'admin@example.com'")
    assert me["totp_secret"].startswith("v1:") and len(codes) == 10  # sekret zaszyfrowany, 10 kodów zapasowych
    client = _fresh(admin)
    first = client.post("/login", data={"email": "admin@example.com", "password": "bardzo-tajne"})
    assert first.headers["HX-Redirect"].startswith("/login/2fa") and "dpl_session" not in first.headers.get("set-cookie", "")
    assert client.post("/login/2fa", data={"code": "000000"}).status_code == 401
    # kod z kroku już użytego przy włączaniu nie przejdzie drugi raz; bierzemy kod z następnego kroku zegara
    code = totp.code_at(secret, totp.now_step() + 1)
    ok = client.post("/login/2fa", data={"code": code, "next": "/apps"})
    assert ok.status_code == 204 and ok.headers["HX-Redirect"] == "/apps" and "dpl_session=" in ok.headers["set-cookie"]
    again = _fresh(admin)
    again.post("/login", data={"email": "admin@example.com", "password": "bardzo-tajne"})
    assert again.post("/login/2fa", data={"code": code}).status_code == 401  # ten sam kod — powtórka odrzucona
    assert again.post("/login/2fa", data={"code": codes[0]}).status_code == 204  # kod zapasowy
    third = _fresh(admin)
    third.post("/login", data={"email": "admin@example.com", "password": "bardzo-tajne"})
    assert third.post("/login/2fa", data={"code": codes[0]}).status_code == 401  # kod zapasowy działa raz


def test_2fa_page_without_pending_login_goes_back(admin):
    plain = TestClient(admin.app, follow_redirects=False)
    assert plain.get("/login/2fa").headers["location"].startswith("/login")


def test_require_2fa_for_everyone_and_admin_reset(admin):
    panel = admin.app.state.panel
    assert admin.post("/settings/users/require-2fa").status_code == 400  # najpierw własne 2FA
    _enable(admin)
    assert admin.post("/settings/users/require-2fa").status_code == 204 and panel.db.setting("require_2fa")
    bob = panel.db.insert("users", email="bob@example.com", password_hash=hash_password("bob-haslo-1"), permissions="", created_at="x", undeletable=0)
    client = TestClient(admin.app, follow_redirects=False)
    client.post("/login", data={"email": "bob@example.com", "password": "bob-haslo-1"})
    assert client.get("/apps").headers["location"] == "/account"  # bez 2FA zostaje tylko Moje konto
    assert client.get("/account").status_code == 200
    panel.db.update("users", bob, totp_secret=panel.sealer.seal(totp.new_secret()))
    assert admin.post(f"/settings/users/{bob}/2fa/reset").status_code == 204
    assert panel.db.get("users", bob)["totp_secret"] is None


def test_sessions_list_and_revoke(admin):
    panel = admin.app.state.panel
    me = panel.db.fetchone("SELECT * FROM users WHERE email = 'admin@example.com'")
    other, _ = panel.sessions.create(me["id"], "198.51.100.3", "Firefox na telefonie")
    page = admin.get("/account").text
    assert "Firefox na telefonie" in page and other not in page  # identyfikator sesji nie trafia do przeglądarki
    handle = next(s["handle"] for s in panel.sessions.of_user(me["id"]) if s["id"] == other)
    assert admin.post(f"/account/sessions/{handle}/revoke").status_code == 204
    assert panel.sessions.load(other) is None
    assert admin.get("/account").status_code == 200  # bieżąca sesja działa dalej

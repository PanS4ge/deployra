"""Konta: przejęcie administratora, obecne hasło przy zmianie własnych danych logowania."""

from __future__ import annotations

import re

from deployra_common.passwords import hash_password, verify_password
from deployra_panel.permissions import Permission, encode


def _login(client, email, password):
    client.headers.pop("X-CSRF-Token", None)
    client.cookies.clear()
    response = client.post("/login", data={"email": email, "password": password})
    assert response.status_code in (204, 303), response.text
    page = client.get("/settings/users").text
    client.headers["X-CSRF-Token"] = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)


def test_user_manager_cannot_take_over_the_admin(admin):
    panel = admin.app.state.panel
    first = panel.db.fetchone("SELECT * FROM users WHERE undeletable = 1")
    perms = encode({Permission.ALLOW_MODIFYING_USERS, Permission.ALLOW_CREATING_USERS_SETTING})
    panel.db.insert("users", email="hr@example.com", password_hash=hash_password("hr-password-1"), permissions=perms, created_at="x", undeletable=0)
    _login(admin, "hr@example.com", "hr-password-1")
    response = admin.post("/settings/users/save", data={"id": first["id"], "email": "evil@example.com", "password": "przejete-haslo"})
    assert response.status_code == 403
    after = panel.db.get("users", first["id"])
    assert after["email"] == "admin@example.com" and verify_password("bardzo-tajne", after["password_hash"])


def test_own_password_change_needs_the_current_one(admin):
    panel = admin.app.state.panel
    me = panel.db.fetchone("SELECT * FROM users WHERE email = 'admin@example.com'")
    other = panel.sessions.create(me["id"], "1.2.3.4", "inna przeglądarka")[0]
    base = {"id": me["id"], "email": me["email"], "password": "nowe-haslo-123"}
    assert admin.post("/settings/users/save", data=base).status_code == 403
    assert admin.post("/settings/users/save", data={**base, "current_password": "zle"}).status_code == 403
    assert admin.post("/settings/users/save", data={**base, "current_password": "bardzo-tajne"}).status_code in (204, 303)
    assert verify_password("nowe-haslo-123", panel.db.get("users", me["id"])["password_hash"])
    assert panel.sessions.load(other) is None  # inne sesje wylogowane
    assert admin.get("/settings/users").status_code == 200  # bieżąca zostaje


def test_api_token_only_for_mcp_expires_and_is_narrowed(admin):
    import time

    from deployra_panel.services import tokens

    panel = admin.app.state.panel
    me = panel.db.fetchone("SELECT * FROM users WHERE email = 'admin@example.com'")
    raw, row = tokens.create_token(panel, "t", me["id"], days=1)
    bare = admin.__class__(admin.app, follow_redirects=False)  # bez ciasteczka sesji
    assert bare.get("/settings/users", headers={"Authorization": f"Bearer {raw}"}).status_code == 303  # poza /mcp token nic nie daje
    assert tokens.verify_token(panel, raw) is not None
    panel.db.update("api_tokens", row["id"], expires_at=time.time() - 1)
    assert tokens.verify_token(panel, raw) is None
    perms = encode({Permission.ALLOW_PROXY})
    assert tokens.narrow(perms, encode({Permission.ALLOW_PROXY, Permission.FULL_ACCESS, Permission.ALLOW_TUNNEL})) == perms


def test_new_token_is_shown_once_and_not_in_url(admin):
    response = admin.post("/settings/mcp/tokens", data={"name": "agent", "days": "30"})
    assert response.status_code in (204, 303) and "dpl_mcp_" not in (response.headers.get("HX-Redirect") or response.headers.get("location") or "")
    full = re.compile(r"dpl_mcp_[A-Za-z0-9_-]{30,}")
    first = full.search(admin.get("/settings/mcp").text)
    assert first and first.group(0) not in admin.get("/settings/mcp").text  # drugi raz już go nie ma


def test_login_limits_survive_restart_and_use_real_ip(admin, config):
    from types import SimpleNamespace

    from deployra_panel.security import LoginLimiter, client_ip

    panel = admin.app.state.panel
    for _ in range(10):
        panel.limiter.fail("203.0.113.5", "admin@example.com")
    again = LoginLimiter(panel.db)  # nowy obiekt = jak po restarcie panelu
    assert again.blocked("203.0.113.5") and not again.blocked("203.0.113.6")
    assert again.delay("admin@example.com") > 0 and again.delay("ktos@example.com") == 0
    request = SimpleNamespace(client=SimpleNamespace(host="10.66.0.7"), headers={"x-forwarded-for": "1.1.1.1, 198.51.100.4"})
    assert client_ip(request, {"10.66.0.7"}) == "198.51.100.4"  # klient dopisał 1.1.1.1 — liczy się wpis od nginx
    assert client_ip(request, set()) == "10.66.0.7"  # niezaufany nadawca: nagłówek ignorowany


def test_security_headers_and_safe_next(client):
    from deployra_panel.web import safe_next

    response = client.get("/login")
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN" and "frame-ancestors" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert safe_next(r"/\evil.com") == "/" and safe_next("//evil.com") == "/" and safe_next("/apps/1?x=2") == "/apps/1?x=2"


def test_host_access_needs_its_own_permission(admin):
    panel = admin.app.state.panel
    node_id = panel.db.insert("nodes", name="n1", transport="direct", api_url="http://127.0.0.1:1", status="paired", secret="x")
    perms = encode({Permission.ALLOW_MODIFY_DOCKERS, Permission.ALLOW_DEPLOYMENT})
    panel.db.insert("users", email="ops@example.com", password_hash=hash_password("ops-password-1"), permissions=perms, created_at="x", undeletable=0)
    _login(admin, "ops@example.com", "ops-password-1")
    container = admin.post(f"/nodes/{node_id}/containers/new", data={"name": "x", "image": "alpine", "volumes": "/:/host"})
    assert container.status_code == 403
    stack = admin.post(f"/nodes/{node_id}/stacks/save", data={"name": "s", "compose": "services:\n  a:\n    image: x\n    privileged: true\n"})
    assert stack.status_code == 403


def test_ssrf_guard_blocks_cloud_metadata():
    import asyncio

    import pytest

    from deployra_panel.services import netguard

    assert netguard.host_blocked("169.254.169.254") and netguard.host_blocked("[fd00:ec2::254]")
    assert not netguard.host_blocked("192.168.1.10") and not netguard.host_blocked("127.0.0.1")
    with pytest.raises(ValueError):
        asyncio.run(netguard.check_url("http://169.254.169.254/latest/meta-data/"))

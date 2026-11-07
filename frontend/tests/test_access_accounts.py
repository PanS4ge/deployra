"""Lista dostępu 2.0: konta listy, logowanie Deployra na hoście aplikacji (przez bramkę), 2FA, blokady, sesje."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from deployra_common import totp
from deployra_common.passwords import apr1
from deployra_panel.db import now
from deployra_panel.services import access_accounts, proxy_state

APP = "files.example.com"
TARGET = {"type": "address", "host": "10.8.0.2", "port": 3923, "scheme": "http", "path": ""}


@pytest.fixture
def acl(admin):
    panel = admin.app.state.panel
    acl_id = panel.db.insert("access_lists", name="Pliki", satisfy_any=0, pass_auth=0, users=[], clients=[], deployra_login=1,
                             login_mode="deployra", login_users=[], allow_panel=0, created_at=now())  # fmt: skip
    panel.db.insert("sites", kind="proxy", domains=[APP], enabled=1, target=TARGET, access_list_id=acl_id, created_at=now())
    account_id = access_accounts.create(panel, acl_id, "mama", "dlugie-haslo-1", "Mama")
    return {"panel": panel, "acl": acl_id, "account": account_id}


def gateway(admin, panel, ip="203.0.113.9") -> TestClient:
    """Klient jak bramka: host aplikacji, token bramki, prawdziwy adres klienta."""
    client = TestClient(admin.app, follow_redirects=False)
    client.headers.update({"Host": APP, "X-Deployra-Host": APP, "X-Deployra-Gw": access_accounts.gw_token(panel), "X-Real-IP": ip,
                           "X-Forwarded-Proto": "https"})  # fmt: skip
    return client


def form_csrf(client, rd="/folder") -> str:
    page = client.get("/auth/forward/login", params={"rd": rd})
    assert page.status_code == 200, page.text
    assert "default-src 'none'" in page.headers["Content-Security-Policy"] and page.headers["X-Frame-Options"] == "DENY"
    token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    client.cookies.set("dpl_fs_csrf", token)  # przeglądarka odsyła je na /__deployra_auth (tu: ścieżka panelu)
    return token


def login(client, username="mama", password="dlugie-haslo-1", rd="/folder"):
    return client.post("/auth/forward/login", data={"username": username, "password": password, "csrf": form_csrf(client, rd), "rd": rd})


def session_cookie(response) -> str:
    return re.search(r"__Host-dpl_fs=([^;]+)", response.headers["set-cookie"]).group(1)


def verify(admin, raw):
    client = TestClient(admin.app, follow_redirects=False)
    client.cookies.set("__Host-dpl_fs", raw)
    return client.get("/auth/forward", headers={"X-Deployra-Host": APP})


def test_login_needs_the_gateway_token_and_a_protected_host(admin, acl):
    bare = TestClient(admin.app, follow_redirects=False)
    assert bare.get("/auth/forward/login", headers={"Host": APP, "X-Deployra-Host": APP}).status_code == 403  # z pominięciem bramki — nie
    client = gateway(admin, acl["panel"])
    client.headers["X-Deployra-Host"] = "inny.example.com"
    assert client.get("/auth/forward/login").status_code == 404


def test_account_login_creates_a_server_side_session(admin, acl):
    client = gateway(admin, acl["panel"])
    response = login(client)
    assert response.status_code == 303 and response.headers["location"] == "/folder"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "Domain=" not in cookie
    raw = session_cookie(response)
    ok = verify(admin, raw)
    assert ok.status_code == 204 and ok.headers["X-Deployra-User"] == "mama"
    row = acl["panel"].db.get("access_accounts", acl["account"])
    assert row["last_login_ip"] == "203.0.113.9"  # X-Real-IP zaufany, bo przyszedł z tokenem bramki
    access_accounts.set_enabled(acl["panel"], acl["account"], False)  # wyłączenie konta kończy sesję od razu
    assert verify(admin, raw).status_code == 401


def test_bad_password_csrf_and_lockout(admin, acl):
    client = gateway(admin, acl["panel"])
    assert login(client, password="zle").status_code == 401
    no_csrf = client.post("/auth/forward/login", data={"username": "mama", "password": "dlugie-haslo-1", "csrf": "x", "rd": "/"})
    assert no_csrf.status_code == 400
    evil = client.post("/auth/forward/login", data={"username": "mama", "password": "dlugie-haslo-1", "csrf": form_csrf(client), "rd": "/"},
                       headers={"Origin": "https://evil.example.com"})  # fmt: skip
    assert evil.status_code == 400
    panel = acl["panel"]
    panel.db.update("access_accounts", acl["account"], failed=access_accounts.LOCK_AFTER - 1)
    panel.fwd_limiter.delay = lambda key: 0  # test: bez rosnącego opóźnienia
    assert login(client, password="zle").status_code == 401
    locked = login(client)  # poprawne hasło też nie przejdzie, dopóki trwa blokada
    assert locked.status_code == 401 and ("zablokowane" in locked.text.lower() or "locked" in locked.text.lower())


def test_totp_step(admin, acl):
    panel = acl["panel"]
    secret = totp.new_secret()
    access_accounts.set_totp(panel, acl["account"], secret)
    client = gateway(admin, panel)
    step = login(client)
    assert step.status_code == 200 and 'name="code"' in step.text
    pre = re.search(r'name="pre" value="([^"]+)"', step.text).group(1)
    csrf = re.search(r'name="csrf" value="([^"]+)"', step.text).group(1)
    client.cookies.set("dpl_fs_csrf", csrf)
    bad = client.post("/auth/forward/totp", data={"code": "000000", "pre": pre, "csrf": csrf, "rd": "/"})
    assert bad.status_code == 401
    csrf = re.search(r'name="csrf" value="([^"]+)"', bad.text).group(1)
    client.cookies.set("dpl_fs_csrf", csrf)
    good = client.post("/auth/forward/totp", data={"code": totp.code_at(secret, totp.now_step()), "pre": pre, "csrf": csrf, "rd": "/"})
    assert good.status_code == 303 and verify(admin, session_cookie(good)).status_code == 204


def test_required_totp_without_secret_is_refused(admin, acl):
    acl["panel"].db.update("access_lists", acl["acl"], require_totp=1)
    assert login(gateway(admin, acl["panel"])).status_code == 403


def test_mode_change_revokes_sessions_and_basic_uses_accounts(admin, acl):
    panel = acl["panel"]
    raw = session_cookie(login(gateway(admin, panel)))
    saved = admin.post("/proxy/access/save", data={"id": str(acl["acl"]), "name": "Pliki", "login_mode": "basic"})
    assert saved.status_code == 204
    assert verify(admin, raw).status_code == 401
    state = proxy_state.build_state(panel)
    lst = next(a for a in state.access_lists if a.id == acl["acl"])
    assert not lst.deployra_login and [u.username for u in lst.users] == ["mama"] and lst.users[0].htpasswd.startswith("$apr1$")


def test_legacy_basic_users_become_accounts(admin):
    panel = admin.app.state.panel
    acl_id = panel.db.insert("access_lists", name="Stara", satisfy_any=0, pass_auth=0, users=[{"username": "tata", "htpasswd": apr1("stare-haslo")}],
                             clients=[], created_at=now())  # fmt: skip
    assert access_accounts.migrate_legacy(panel) == 1
    assert panel.db.get("access_lists", acl_id)["users"] == []
    account, _ = access_accounts.authenticate(panel, acl_id, "tata", "stare-haslo", "1.2.3.4")
    assert account and panel.db.get("access_accounts", account["id"])["password_hash"].startswith("scrypt$")  # hash przeliczony


def test_account_management_routes(admin, acl):
    base = f"/proxy/access/{acl['acl']}/accounts"
    assert admin.post(base, data={"username": "zly login", "password": "dlugie-haslo-2"}).status_code == 400
    assert admin.post(base, data={"username": "syn", "password": "dlugie-haslo-2", "name": "Syn"}).status_code == 204
    panel = acl["panel"]
    syn = panel.db.fetchone("SELECT * FROM access_accounts WHERE username = 'syn'")
    reveal = admin.post(f"{base}/{syn['id']}/totp")
    assert "otpauth://totp/" in reveal.text and panel.db.get("access_accounts", syn["id"])["totp_secret"].startswith("v1:")  # zaszyfrowany
    assert admin.post(f"{base}/{syn['id']}/password", data={"password": "krotkie"}).status_code == 400
    assert admin.post(f"{base}/{syn['id']}/delete").status_code == 204
    assert panel.db.get("access_accounts", syn["id"]) is None
    other = panel.db.insert("access_lists", name="Inna", satisfy_any=0, pass_auth=0, users=[], clients=[], created_at=now())
    assert admin.post(f"/proxy/access/{other}/accounts/{acl['account']}/delete").status_code == 404  # konto innej listy

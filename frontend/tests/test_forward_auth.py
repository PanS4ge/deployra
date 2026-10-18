"""Logowanie Deployra przed aplikacją: /auth/forward, /auth/forward/start, /auth/forward/callback i lista dostępu."""

import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from deployra_panel.db import now
from deployra_panel.services import forward_auth, proxy_state

APP = "app.example.com"
TARGET = {"type": "address", "host": "10.0.0.5", "port": 8080, "scheme": "http", "path": ""}


class Clock:
    """Zegar do testów wygaśnięcia — podmienia `time` w module forward_auth."""

    def __init__(self, at):
        self.at = at

    def time(self):
        return self.at


@pytest.fixture
def clock(monkeypatch):
    fake = Clock(time.time())
    monkeypatch.setattr(forward_auth, "time", fake)
    return fake


@pytest.fixture
def setup_login(admin):
    """Konto admina, drugie konto i lista z logowaniem Deployra przypięta do hosta app.example.com."""
    panel = admin.app.state.panel
    admin_id = panel.db.fetchone("SELECT id FROM users WHERE email = ?", ["admin@example.com"])["id"]
    bob_id = panel.db.insert("users", email="bob@example.com", password_hash="x", name="Bob", surname="", permissions="",
                             undeletable=0, created_at=now())  # fmt: skip
    acl_id = panel.db.insert("access_lists", name="Deployra", satisfy_any=0, pass_auth=0, users=[], clients=[], deployra_login=1,
                             login_users=[], created_at=now())  # fmt: skip
    site_id = panel.db.insert("sites", kind="proxy", domains=[APP], enabled=1, target=TARGET, access_list_id=acl_id, created_at=now())
    return {"panel": panel, "admin": admin_id, "bob": bob_id, "acl": acl_id, "site": site_id}


def anon(admin) -> TestClient:
    """Klient bez sesji panelu — jak bramka i przeglądarka na hoście aplikacji."""
    return TestClient(admin.app, follow_redirects=False)


def verify(client, host=APP, cookie=None):
    if cookie is not None:
        client.cookies.set(forward_auth.FORWARD_COOKIE, cookie)
    return client.get("/auth/forward", headers={"X-Deployra-Host": host})


def start(admin, rd):
    return admin.get("/auth/forward/start", params={"rd": rd}, headers={"HX-Request": ""})


def token_of(location):
    return parse_qs(urlsplit(location).query)["token"][0]


def test_verify_needs_valid_cookie_for_the_same_host(admin, setup_login):
    panel = setup_login["panel"]
    client = anon(admin)
    assert verify(client).status_code == 401  # bez ciasteczka
    cookie = forward_auth.make_cookie(panel, setup_login["admin"], APP)
    response = verify(client, cookie=cookie)
    assert response.status_code == 204 and response.headers["X-Deployra-User"] == "admin@example.com"
    assert "set-cookie" not in response.headers  # sprawdzenie nie wystawia ciasteczek
    assert verify(client, host="other.example.com", cookie=cookie).status_code == 401  # ciasteczko związane z hostem
    assert client.head("/auth/forward", headers={"X-Deployra-Host": APP}).status_code == 204  # bramka może użyć HEAD


def test_tampered_or_foreign_cookies_are_rejected(admin, setup_login):
    panel = setup_login["panel"]
    client = anon(admin)
    good = forward_auth.make_cookie(panel, setup_login["admin"], APP)
    payload, signature = good.split(".")
    flipped_payload = payload[:-2] + ("AA" if payload[-2:] != "AA" else "BB")
    assert verify(client, cookie=f"{flipped_payload}.{signature}").status_code == 401  # zmieniona treść
    assert verify(client, cookie=f"{payload}.{signature[:-2]}AA").status_code == 401  # zmieniony podpis
    assert verify(client, cookie="nonsense").status_code == 401
    assert verify(client, cookie="a.b.c").status_code == 401
    bob = forward_auth.make_cookie(panel, setup_login["bob"], APP)
    assert verify(client, cookie=f"{bob.split('.')[0]}.{signature}").status_code == 401  # podpis nie pasuje do treści
    panel.sealer._key = b"\x01" * 32  # inny sekret panelu → wcześniejsze ciasteczka są nieważne
    assert verify(client, cookie=good).status_code == 401


def test_cookie_expires_and_follows_the_access_list(admin, setup_login, clock):
    panel = setup_login["panel"]
    client = anon(admin)
    cookie = forward_auth.make_cookie(panel, setup_login["admin"], APP)
    assert verify(client, cookie=cookie).status_code == 204
    clock.at += panel.config.session_hours * 3600 + 1
    assert verify(client, cookie=cookie).status_code == 401  # wygasło
    # lista z dozwolonymi użytkownikami: konto spoza listy dostaje 401, mimo ważnego podpisu
    panel.db.update("access_lists", setup_login["acl"], login_users=[setup_login["bob"]])
    assert verify(client, cookie=forward_auth.make_cookie(panel, setup_login["admin"], APP)).status_code == 401
    bob = forward_auth.make_cookie(panel, setup_login["bob"], APP)
    assert verify(client, cookie=bob).status_code == 204
    panel.db.delete("users", setup_login["bob"])  # usunięte konto traci dostęp od razu
    assert verify(client, cookie=bob).status_code == 401


def test_site_without_login_list_is_not_affected(admin, setup_login):
    panel = setup_login["panel"]
    plain = panel.db.insert("access_lists", name="Plain", satisfy_any=0, pass_auth=0, users=[], clients=[], deployra_login=0,
                            login_users=[], created_at=now())  # fmt: skip
    panel.db.insert("sites", kind="proxy", domains=["plain.example.com"], enabled=1, target=TARGET, access_list_id=plain, created_at=now())
    cookie = forward_auth.make_cookie(panel, setup_login["admin"], "plain.example.com")
    assert verify(anon(admin), host="plain.example.com", cookie=cookie).status_code == 401


@pytest.mark.parametrize(
    "rd, host",
    [
        ("https://app.example.com/", APP),
        ("https://app.example.com/panel/index?x=1", APP),
        ("http://app.example.com/a", APP),
        ("https://evil.example.com/", "evil.example.com"),
        ("https://app.example.com.evil.com/", "app.example.com.evil.com"),
        ("https://app.example.com@evil.com/", None),
        ("https://user@app.example.com/", None),
        ("https://app.example.com:8443/", None),
        ("https://app.example.com//evil.com", None),
        ("https://app.example.com/a b", None),
        ("https://app.example.com/a\\b", None),
        ("javascript:alert(1)", None),
        ("/relative", None),
        ("", None),
    ],
)
def test_rd_parsing_is_strict(rd, host):
    target = forward_auth.parse_rd(rd)
    if host is None:
        assert target is None
    else:
        assert target is not None and target[0] == host


def test_start_issues_token_only_for_logged_in_allowed_user(admin, setup_login):
    ok = start(admin, f"https://{APP}/panel?a=1")
    assert ok.status_code == 302
    location = urlsplit(ok.headers["location"])
    assert (location.scheme, location.netloc, location.path) == ("https", APP, "/__deployra_auth/callback")
    assert token_of(ok.headers["location"])
    for rd in ("https://evil.example.com/", "https://app.example.com:8443/", "https://app.example.com@evil.com/"):
        refused = start(admin, rd)
        assert refused.status_code == 303 and refused.headers["location"] == "/"
    setup_login["panel"].db.update("access_lists", setup_login["acl"], login_users=[setup_login["bob"]])
    assert start(admin, f"https://{APP}/").status_code == 303  # admin nie jest na liście
    anonymous = start(anon(admin), f"https://{APP}/")
    assert "__deployra_auth" not in anonymous.headers.get("location", "")  # bez sesji nie ma tokenu


def test_callback_exchanges_one_time_token_for_host_cookie(admin, setup_login):
    token = token_of(start(admin, f"https://{APP}/panel?a=1").headers["location"])
    client = anon(admin)
    response = client.get("/auth/forward/callback", params={"token": token}, headers={"Host": APP})
    assert response.status_code == 302 and response.headers["location"] == f"https://{APP}/panel"
    cookie_header = response.headers["set-cookie"]
    assert cookie_header.startswith(f"{forward_auth.FORWARD_COOKIE}=")
    assert "HttpOnly" in cookie_header and "Secure" in cookie_header and "samesite=lax" in cookie_header.lower()
    assert "Domain=" not in cookie_header  # ciasteczko tylko dla hosta aplikacji
    assert "dpl_session" not in cookie_header  # ciasteczko sesji panelu nie wycieka na host aplikacji
    again = client.get("/auth/forward/callback", params={"token": token}, headers={"Host": APP})
    assert again.status_code == 401  # token jest jednorazowy


def test_callback_rejects_expired_forged_and_foreign_tokens(admin, setup_login, clock):
    token = token_of(start(admin, f"https://{APP}/").headers["location"])
    client = anon(admin)
    assert client.get("/auth/forward/callback", params={"token": token}, headers={"Host": "other.example.com"}).status_code == 401
    forged = token[:-3] + ("AAA" if token[-3:] != "AAA" else "BBB")
    assert client.get("/auth/forward/callback", params={"token": forged}, headers={"Host": APP}).status_code == 401
    assert client.get("/auth/forward/callback", headers={"Host": APP}).status_code == 401  # bez tokenu
    clock.at += forward_auth.TOKEN_TTL + 1
    assert client.get("/auth/forward/callback", params={"token": token}, headers={"Host": APP}).status_code == 401


def test_token_signed_with_other_secret_is_rejected(admin, setup_login):
    panel = setup_login["panel"]
    token = forward_auth.make_token(panel, setup_login["admin"], APP, "/")
    panel.sealer._key = b"\x02" * 32
    assert forward_auth.consume_token(panel, token, APP) is None
    payload, signature = token.split(".")
    assert forward_auth.consume_token(panel, f"{payload}.{signature[::-1]}", APP) is None


def test_login_page_resumes_forward_flow(admin, setup_login):
    next_url = f"/auth/forward/start?rd=https://{APP}/x"
    response = admin.get("/login", params={"next": next_url}, headers={"HX-Request": ""})
    assert response.status_code == 303 and response.headers["location"] == next_url
    assert admin.get("/login", params={"next": "//evil.example.com"}, headers={"HX-Request": ""}).headers["location"] == "/"


def test_panel_auth_addresses_follow_tunnel_and_domain(admin, setup_login, monkeypatch):
    panel = setup_login["panel"]
    monkeypatch.setattr(panel, "gateway", lambda: {"panel_address": "10.66.0.9/24"})
    state = proxy_state.build_state(panel)
    assert state.panel_auth is not None
    assert state.panel_auth.verify == "http://10.66.0.9:8080/auth/forward"
    assert state.panel_auth.callback == "http://10.66.0.9:8080/auth/forward/callback"
    assert state.panel_auth.login == "https://deploy.example.com/login"
    assert state.access_lists[0].deployra_login is True
    panel.db.update("access_lists", setup_login["acl"], deployra_login=0)
    assert proxy_state.build_state(panel).panel_auth is None  # bez list z logowaniem nie ma czego przekazywać


def test_access_list_form_saves_login_options(admin, setup_login):
    panel = setup_login["panel"]
    page = admin.get(f"/proxy/access/{setup_login['acl']}", headers={"HX-Request": ""}).text
    assert 'name="deployra_login"' in page and 'name="login_users"' in page and "bob@example.com" in page
    saved = admin.post("/proxy/access/save", data={
        "id": str(setup_login["acl"]), "name": "Deployra", "satisfy_any": "1", "deployra_login": "1",
        "login_users": [str(setup_login["bob"]), "9999"],
    })  # fmt: skip
    assert saved.status_code == 204, saved.text
    row = panel.db.get("access_lists", setup_login["acl"])
    assert row["deployra_login"] == 1 and row["login_users"] == [setup_login["bob"]]  # nieznane konta pominięte
    # hasło Basic razem z logowaniem Deployra tylko w trybie „wystarczy jeden warunek”
    conflict = admin.post("/proxy/access/save", data={
        "id": str(setup_login["acl"]), "name": "Deployra", "deployra_login": "1", "users": "bob:tajne-haslo",
    })  # fmt: skip
    assert conflict.status_code == 400
    assert panel.db.get("access_lists", setup_login["acl"])["deployra_login"] == 1  # poprzedni stan bez zmian
    off = admin.post("/proxy/access/save", data={"id": str(setup_login["acl"]), "name": "Deployra", "login_users": [str(setup_login["bob"])]})
    assert off.status_code == 204
    row = panel.db.get("access_lists", setup_login["acl"])
    assert row["deployra_login"] == 0 and row["login_users"] == []

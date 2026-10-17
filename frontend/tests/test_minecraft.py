import asyncio
import json
import time

from deployra_common import hmac_auth
from deployra_common.schemas import AppSpec
from deployra_panel.security import SESSION_COOKIE
from deployra_panel.services import deploy
from deployra_panel.services import minecraft as mc

POLL = "/minecraft/bridge/poll"


def signed_poll(client, server_id, secret, payload, headers=None):
    body = json.dumps(payload).encode()
    headers = headers or hmac_auth.sign(bytes.fromhex(secret), "POST", POLL, body, key_id=str(server_id))
    return client.post(POLL, content=body, headers={**headers, "Content-Type": "application/json"}), headers


def paired_server(panel, client, name="Survival"):
    server_id, code = mc.create(panel, name)
    response = client.post("/minecraft/bridge/pair", json={"code": code, "info": {"software": "Paper", "mc_version": "1.21.1"}})
    assert response.status_code == 200
    return response.json()


def test_pairing_is_one_time_and_expires(panel, admin):
    client = admin
    server_id, code = mc.create(panel, "Test")
    assert mc.pending_code(panel, mc.server(panel, server_id)) == code  # widoczny w panelu, póki nie użyty
    first = client.post("/minecraft/bridge/pair", json={"code": code})
    assert first.status_code == 200 and first.json()["server_id"] == server_id and len(first.json()["secret"]) == 64
    assert client.post("/minecraft/bridge/pair", json={"code": code}).status_code == 403  # drugi raz nie zadziała
    row = mc.server(panel, server_id)
    assert row["pair_code"] is None and row["secret"] and first.json()["secret"] not in row["secret"]  # sekret zaszyfrowany

    _, old = mc.create(panel, "Old")
    panel.db.execute("UPDATE minecraft_servers SET pair_expires = ? WHERE name = 'Old'", [time.time() - 1])
    assert client.post("/minecraft/bridge/pair", json={"code": old}).status_code == 403


def test_pairing_attempts_are_rate_limited(panel, admin):
    client = admin
    mc._pair_attempts.clear()
    for _ in range(10):
        assert client.post("/minecraft/bridge/pair", json={"code": "zgaduje"}).status_code == 403
    _, code = mc.create(panel, "Late")
    assert client.post("/minecraft/bridge/pair", json={"code": code}).status_code == 403  # limit na IP, nawet z dobrym kodem
    mc._pair_attempts.clear()


def test_poll_requires_valid_fresh_unique_signature(panel, admin, monkeypatch):
    client = admin
    monkeypatch.setattr(mc, "POLL_SECONDS", 0.01)
    paired = paired_server(panel, client)
    ok, headers = signed_poll(client, paired["server_id"], paired["secret"], {"info": {"players_online": 3}})
    assert ok.status_code == 200 and ok.json() == {"commands": []}
    # odpowiedź panelu też jest podpisana — plugin nie wykona podrzuconego polecenia
    assert hmac_auth.H_SIG in ok.headers and ok.headers[hmac_auth.H_KEY] == "panel"
    bound = f"{POLL}#{headers[hmac_auth.H_NONCE]}"  # podpis odpowiedzi związany z nonce żądania
    canonical = hmac_auth._canonical("RESPONSE", bound, int(ok.headers[hmac_auth.H_TS]), ok.headers[hmac_auth.H_NONCE], __import__("hashlib").sha256(ok.content).hexdigest())
    assert __import__("hmac").new(bytes.fromhex(paired["secret"]), canonical, "sha256").hexdigest() == ok.headers[hmac_auth.H_SIG]
    assert mc.info(mc.server(panel, paired["server_id"]))["players_online"] == 3

    replay, _ = signed_poll(client, paired["server_id"], paired["secret"], {"info": {"players_online": 3}}, headers=headers)
    assert replay.status_code == 401  # powtórzony nonce
    forged, _ = signed_poll(client, paired["server_id"], "00" * 32, {})
    assert forged.status_code == 401
    stale = hmac_auth.sign(bytes.fromhex(paired["secret"]), "POST", POLL, b"{}", key_id=str(paired["server_id"]), ts=int(time.time()) - 3600)
    assert client.post(POLL, content=b"{}", headers=stale).status_code == 401


def test_command_round_trip_through_long_poll(panel, admin):
    client = admin
    paired = paired_server(panel, client)
    row = mc.server(panel, paired["server_id"])

    mc._links.clear()  # stan kanału żyje w jednej pętli zdarzeń — test ma własną

    async def scenario():
        mc.link(row["id"]).seen = time.time()
        call = asyncio.create_task(mc.call(panel, row, "command", {"command": "list"}))
        commands = await asyncio.wait_for(mc.poll(panel, row, {}), 2)  # polecenie wraca od razu, bez czekania 25 s
        assert [c["op"] for c in commands] == ["command"] and commands[0]["args"] == {"command": "list"}
        waiting = asyncio.create_task(mc.poll(panel, row, {"results": [{"id": commands[0]["id"], "ok": True, "data": {"output": ["2 players"]}}]}))
        result = await asyncio.wait_for(call, 2)
        waiting.cancel()
        return result

    assert asyncio.run(scenario()) == {"output": ["2 players"]}


def test_offline_server_and_validation(panel, admin):
    server_id, _ = mc.create(panel, "Off")
    assert admin.post(f"/minecraft/{server_id}/command", data={"command": "say hi"}).status_code == 409
    assert admin.post(f"/minecraft/{server_id}/command", data={"command": ""}).status_code == 400
    assert admin.post(f"/minecraft/{server_id}/player", data={"name": "Notch;op @a", "action": "op"}).status_code == 400
    assert admin.post(f"/minecraft/{server_id}/player", data={"name": "Notch", "action": "rm -rf"}).status_code == 400


def test_permissions_required(panel, admin):
    client = admin
    server_id, _ = mc.create(panel, "Locked")
    user = panel.db.insert("users", email="viewer@deployra.local", password_hash="hash", name="V", permissions="E")  # tylko podgląd
    sid, csrf = panel.sessions.create(user, "127.0.0.1", "pytest")
    client.cookies.set(SESSION_COOKIE, sid)
    headers = {"X-CSRF-Token": csrf, "HX-Request": "true"}
    assert client.get("/minecraft", headers={"HX-Request": ""}).status_code == 200
    assert client.post(f"/minecraft/{server_id}/command", data={"command": "op Steve"}, headers=headers).status_code == 403
    assert client.post(f"/minecraft/{server_id}/properties", data={"p:pvp": "false"}, headers=headers).status_code == 403
    assert client.post(f"/minecraft/{server_id}/delete", headers=headers).status_code == 403
    assert client.post("/minecraft/add", data={"name": "x"}, headers=headers).status_code == 403


def test_properties_booleans_can_be_switched_off(panel, admin, monkeypatch):
    server_id, _ = mc.create(panel, "Props")
    sent = []

    async def fake_call(_panel, _row, op, args=None, timeout=20):
        sent.append((op, args))
        return {}

    monkeypatch.setattr(mc, "call", fake_call)
    response = admin.post(f"/minecraft/{server_id}/properties", data={"p:pvp": "false", "p:white-list": ["false", "true"], "p:motd": "Hej\nświat"})
    assert response.status_code == 200
    assert sent == [("properties.set", {"properties": {"pvp": "false", "white-list": "true", "motd": "Hej świat"}})]


def test_legacy_servers_are_imported_without_plaintext(panel, admin):
    panel.db.set_setting("minecraft_servers", [{"id": 1, "name": "Stary", "host": "1.2.3.4", "token": "jawny-token", "ftp_host": "ftp.example",
                                                 "ftp_user": "u", "ftp_pass": "tajne-haslo"}])  # fmt: skip
    page = admin.get("/minecraft", headers={"HX-Request": ""}).text
    assert "Stary" in page and panel.db.setting("minecraft_servers") is None
    row = panel.db.fetchone("SELECT * FROM minecraft_servers WHERE name = 'Stary'")
    assert "tajne-haslo" not in json.dumps(row) and mc.get_ftp(panel, row)["password"] == "tajne-haslo"
    assert row["secret"] is None  # stary token był jawny — trzeba sparować od nowa


def test_minecraft_template_pairs_app_automatically(panel, admin, monkeypatch):
    node_id = panel.db.insert("nodes", name="n1", transport="direct", api_url="http://127.0.0.1:1", status="paired")
    monkeypatch.setattr("deployra_panel.routes.apps.start_deploy", lambda panel, app: type("J", (), {"id": "job1"})())
    tpl = panel.plugins.app_template("apps-catalog:minecraft")
    assert tpl and "25565:25565" in tpl["source"]["compose"]
    form = {"name": "Kostka", "slug": "kostka", "node_id": str(node_id), "kind": "compose", "compose": tpl["source"]["compose"],
            "expose_service": "minecraft", "container_port": "25565", "env": tpl["env"], "memory_mb": "1536"}  # fmt: skip
    response = admin.post("/apps/new", data=form)
    assert response.status_code in (200, 204, 303), response.text[:300]
    app = panel.db.get("apps", "kostka", key="slug")
    spec, _ = deploy.load(panel, app)
    row = panel.db.fetchone("SELECT * FROM minecraft_servers WHERE app_id = ?", [app["id"]])
    assert row and spec.env["DEPLOYRA_PAIR_CODE"] == mc.pending_code(panel, row)
    assert spec.env["DEPLOYRA_PANEL_URL"] == "https://deploy.example.com"
    page = admin.get(f"/apps/{app['id']}", headers={"HX-Request": ""}).text
    assert f"/minecraft/{row['id']}" in page


def test_plugin_download_is_public(admin):
    admin.cookies.clear()  # bez sesji, jak kontener itzg pobierający plugin
    response = admin.get("/minecraft/download/DeployraBridge.jar")
    assert response.status_code == 200 and response.content[:2] == b"PK"  # itzg pobiera plugin bez logowania


def test_pair_code_only_for_managers(panel, admin):
    server_id, code = mc.create(panel, "Secret")
    assert code in admin.get(f"/minecraft/{server_id}", headers={"HX-Request": ""}).text
    user = panel.db.insert("users", email="viewer2@deployra.local", password_hash="hash", name="V", permissions="E")
    sid, _ = panel.sessions.create(user, "127.0.0.1", "pytest")
    admin.cookies.set(SESSION_COOKIE, sid)
    page = admin.get(f"/minecraft/{server_id}?tab=settings", headers={"HX-Request": ""}).text
    assert code not in page and "ftp_host" not in page  # podgląd nie daje kodu parowania ani ustawień FTP

"""WebSSH: klucz hosta TOFU (sprawdzany przed hasłem), zmieniony klucz = odmowa, ponowne potwierdzenie, sesja w terminalu."""

from __future__ import annotations

import asyncio
import threading

import asyncssh
import pytest

from deployra_panel.services import node_ssh


class _Server(asyncssh.SSHServer):
    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        return username == "ops" and password == "haslo-ssh-1"


async def _echo(process):
    process.stdout.write("witaj\r\n")
    async for line in process.stdin:
        process.stdout.write(f"echo:{line}")
        if "exit" in line:
            break
    process.exit(0)


class SshServer:
    """Serwer SSH w osobnym wątku (panel ma własną pętlę zdarzeń w TestClient)."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.key = asyncssh.generate_private_key("ssh-ed25519")
        self.port = None
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.start()

    def start(self):
        async def run():
            self.server = await asyncssh.create_server(_Server, "127.0.0.1", self.port or 0, server_host_keys=[self.key], process_factory=_echo)
            self.port = self.server.sockets[0].getsockname()[1]

        asyncio.run_coroutine_threadsafe(run(), self.loop).result(10)

    def rotate_key(self):
        async def stop():
            self.server.close()
            await self.server.wait_closed()

        asyncio.run_coroutine_threadsafe(stop(), self.loop).result(10)
        self.key = asyncssh.generate_private_key("ssh-ed25519")
        self.start()


@pytest.fixture(scope="module")
def sshd():
    return SshServer()


@pytest.fixture
def node(admin, sshd):
    panel = admin.app.state.panel
    node_id = panel.db.insert("nodes", name="vps", transport="direct", api_url="http://127.0.0.1:2750", status="paired", secret="x")
    node = panel.node(node_id)
    node_ssh.save(panel, node, {"host": "127.0.0.1", "port": str(sshd.port), "username": "ops", "auth": "password", "password": "haslo-ssh-1"})
    return panel, node_id


def test_secrets_are_sealed_and_never_rendered(admin, node):
    panel, node_id = node
    raw = panel.db.get("nodes", node_id)["ssh"]
    assert raw["secret"].startswith("v1:") and "haslo-ssh-1" not in str(raw)
    page = admin.get(f"/nodes/{node_id}/settings", headers={"HX-Request": ""}).text
    assert "haslo-ssh-1" not in page and 'name="username" value="ops"' in page


def test_unknown_then_trusted_then_changed_host_key(admin, node, sshd):
    panel, node_id = node
    with pytest.raises(node_ssh.HostKeyError) as unknown:
        asyncio.run(node_ssh.connect(panel, panel.node(node_id)))
    assert not unknown.value.changed and unknown.value.fingerprint == sshd.key.get_fingerprint("sha256")
    shown = admin.post(f"/nodes/{node_id}/ssh/hostkey").text
    assert unknown.value.fingerprint in shown
    assert admin.post(f"/nodes/{node_id}/ssh/hostkey/accept", data={"fingerprint": "SHA256:inny"}).status_code == 409
    assert admin.post(f"/nodes/{node_id}/ssh/hostkey/accept", data={"fingerprint": unknown.value.fingerprint}).status_code == 204

    async def login():
        conn = await node_ssh.connect(panel, panel.node(node_id))
        async with conn:
            result = await conn.run("whoami", input="exit\n")
            return result.stdout

    assert "witaj" in asyncio.run(login())
    sshd.rotate_key()  # serwer z innym kluczem (jak podszywający się host)
    try:
        with pytest.raises(node_ssh.HostKeyError) as changed:
            asyncio.run(node_ssh.connect(panel, panel.node(node_id)))
        assert changed.value.changed
    finally:
        node_ssh.accept_key(panel, panel.node(node_id), sshd.key.get_fingerprint("sha256"), sshd.key.export_public_key("openssh").decode())


def test_websocket_needs_permission_reauth_and_bridges_the_shell(admin, node, sshd):
    panel, node_id = node
    node_ssh.accept_key(panel, panel.node(node_id), sshd.key.get_fingerprint("sha256"), sshd.key.export_public_key("openssh").decode())
    page = admin.get(f"/nodes/{node_id}/ssh", headers={"HX-Request": ""}).text
    assert 'name="password"' in page  # najpierw potwierdzenie hasłem
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as refused:
        with admin.websocket_connect(f"/nodes/{node_id}/ssh/ws"):
            pass
    assert refused.value.code == 4428  # bez świeżego potwierdzenia hasłem / 2FA
    assert admin.post(f"/nodes/{node_id}/ssh/reauth", data={"password": "zle"}).status_code == 403
    assert admin.post(f"/nodes/{node_id}/ssh/reauth", data={"password": "bardzo-tajne"}).status_code in (204, 303)
    with admin.websocket_connect(f"/nodes/{node_id}/ssh/ws?cols=80&rows=24") as ws:
        initial = b""
        for _ in range(5):
            initial += ws.receive_bytes()
            if b"witaj" in initial:
                break
        assert b"witaj" in initial

        ws.send_bytes(b"ls\n")
        received = b""
        for _ in range(5):
            received += ws.receive_bytes()
            if b"echo:ls" in received:
                break
        assert b"echo:ls" in received


def test_webssh_button_is_disabled_without_settings(admin):
    panel = admin.app.state.panel
    node_id = panel.db.insert("nodes", name="bez-ssh", transport="direct", api_url="http://127.0.0.1:1", status="paired", secret="x")
    page = admin.get(f"/nodes/{node_id}", headers={"HX-Request": ""}).text
    assert "WebSSH" in page and f'href="/nodes/{node_id}/ssh"' not in page and "disabled" in page

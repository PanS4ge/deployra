import io
import stat
import zipfile
from pathlib import Path

import asyncssh
import pytest

from deployra_backend.services import compose, sftp_server, sftp_users, static_sites, wg_spoke
from deployra_common import wg
from deployra_common.jobs import JobFailed


def make_zip(entries: dict[str, bytes], symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
    return buf.getvalue()


def test_publish_zip_strips_single_top_dir(tmp_path):
    archive = tmp_path / "a.zip"
    archive.write_bytes(make_zip({"dist/index.html": b"<h1>hej</h1>", "dist/css/a.css": b"x"}))
    sites = tmp_path / "sites"
    sites.mkdir()
    assert static_sites.publish_zip(sites, "blog", archive, 10_000_000) == 2
    assert (sites / "blog" / "index.html").read_bytes() == b"<h1>hej</h1>"
    assert (sites / "blog" / "css" / "a.css").exists()
    assert not list(sites.glob(".new-*"))


@pytest.mark.parametrize(
    "entries,symlink",
    [({"../evil.txt": b"x"}, None), ({"/etc/evil": b"x"}, None), ({"ok.txt": b"x"}, "link")],
)
def test_publish_zip_rejects_dangerous_archives(tmp_path, entries, symlink):
    archive = tmp_path / "a.zip"
    archive.write_bytes(make_zip(entries, symlink))
    sites = tmp_path / "sites"
    sites.mkdir()
    (sites / "blog").mkdir()
    (sites / "blog" / "index.html").write_text("stara")
    with pytest.raises(JobFailed):
        static_sites.publish_zip(sites, "blog", archive, 10_000_000)
    assert (sites / "blog" / "index.html").read_text() == "stara"  # stara wersja nietknięta


def test_static_site_conf():
    conf = static_sites.render_site_conf("blog", ["blog.example.com", "www.blog.example.com"])
    assert "server_name blog.example.com www.blog.example.com blog.deployra.internal;" in conf
    assert "root /srv/sites/blog;" in conf
    with pytest.raises(ValueError):
        static_sites.render_site_conf("blog", ["evil.com; root /"])


def test_compose_write_read_and_override(tmp_path):
    yaml_text = "services:\n  web:\n    image: nginx\n  db:\n    image: postgres\n"
    with pytest.raises(ValueError, match="services"):
        compose.parse_compose("version: '3'\n")
    with pytest.raises(ValueError, match="brak usługi"):
        compose.parse_compose(yaml_text, "api")
    env = {"PASS": "a'b\"c$d", "SIMPLE": "x y"}
    override = compose.expose_override("web", 20001, 80, "shop")
    stack_dir = compose.write_stack(tmp_path, "shop", yaml_text, env, override)
    assert compose.read_stack(tmp_path, "shop")["env"] == env
    assert "20001:80" in (stack_dir / compose.OVERRIDE_FILE).read_text()
    cmd = compose.compose_cmd(stack_dir, "shop", "up", "-d")
    assert cmd[:4] == ["docker", "compose", "-p", "shop"] and "--env-file" in cmd and cmd[-2:] == ["up", "-d"]
    with pytest.raises(ValueError):
        compose.write_stack(tmp_path, "Zła Nazwa", yaml_text)
    with pytest.raises(ValueError):
        compose.write_stack(tmp_path, "ok", yaml_text, {"A": "x\nB=y"})


def test_wg_spoke_config_with_lan_routing():
    priv, _ = wg.genkey()
    _, hub = wg.genkey()
    conf = {
        "private_key": priv, "address": "10.66.0.5", "subnet": "10.66.0.0/24", "hub_public_key": hub,
        "hub_endpoint": "vps.example.com:51820", "keepalive": 25, "lan_subnets": ["192.168.1.0/24"],
    }  # fmt: skip
    text = wg_spoke.build_config(conf, lan_iface="eth0")
    assert "Address = 10.66.0.5/24" in text
    assert "PostUp = sysctl -w net.ipv4.ip_forward=1 || true" in text
    assert "PostUp = iptables -A FORWARD -i %i -j ACCEPT" in text and "PostUp = iptables -A FORWARD -o %i -j ACCEPT" in text
    assert "AllowedIPs = 10.66.0.0/24" in text and "PersistentKeepalive = 25" in text
    assert "iptables -t nat -A POSTROUTING -s 10.66.0.0/24 -d 192.168.1.0/24 -o eth0 -j MASQUERADE" in text
    assert "0.0.0.0/0" not in text


class _State(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)

    def mutate(self, key, fn):
        self[key] = fn(dict.get(self, key, {}))
        return self[key]


@pytest.mark.anyio
async def test_sftp_chroot_and_auth(tmp_path):
    state = _State(sftp_users={})
    sites = tmp_path / "sites"
    (sites / "blog").mkdir(parents=True)
    (tmp_path / "sekret.txt").write_text("nie dla ciebie")
    sftp_users.set_user(state, "blogger", "blog", "tajne-hasło-123", None)
    with pytest.raises(ValueError):
        sftp_users.set_user(state, "Zły Login", "blog", "x", None)

    service = sftp_server.SftpService(state, sites, tmp_path / "hostkey")
    await service.start("127.0.0.1", 0)
    port = service._acceptor.sockets[0].getsockname()[1]
    try:
        async with asyncssh.connect(
            "127.0.0.1", port, username="blogger", password="tajne-hasło-123", known_hosts=None
        ) as conn:
            async with conn.start_sftp_client() as sftp:
                async with sftp.open("index.html", "w") as fh:
                    await fh.write("<h1>z SFTP</h1>")
                assert await sftp.listdir("..") == await sftp.listdir("/")  # nie da się wyjść wyżej
                assert "sekret.txt" not in await sftp.listdir("../..")
            with pytest.raises((asyncssh.ChannelOpenError, asyncssh.ProcessError, asyncssh.Error)):
                await conn.run("cat /etc/passwd", check=True)
        assert (sites / "blog" / "index.html").read_text() == "<h1>z SFTP</h1>"

        with pytest.raises(asyncssh.PermissionDenied):
            async with asyncssh.connect("127.0.0.1", port, username="blogger", password="złe", known_hosts=None):
                pass
        assert service.fingerprint().startswith("SHA256:")
    finally:
        await service.stop()


FIB_TRIE = """Main:
  +-- 0.0.0.0/0 3 0 5
     |-- 0.0.0.0
        /0 universe UNICAST
     +-- 127.0.0.0/8 2 0 2
        +-- 127.0.0.0/31 1 0 0
           |-- 127.0.0.1
              /32 host LOCAL
     +-- 192.168.1.0/24 2 0 2
        |-- 192.168.1.0
           /24 link UNICAST
        |-- 192.168.1.50
           /32 host LOCAL
"""


def test_proxmox_discovers_running_cts_with_docker(tmp_path, monkeypatch):
    from deployra_backend.services import endpoints

    proc, pve = tmp_path / "proc", tmp_path / "pve"
    pve.mkdir()
    (pve / "101.conf").write_text("arch: amd64\nhostname: nextcloud\nnet0: name=eth0\n[snap1]\nhostname: stare\n")

    def proc_entry(pid, cgroup, docker=False, fib=None):
        d = proc / str(pid)
        (d / "net").mkdir(parents=True)
        (d / "cgroup").write_text(cgroup)
        if docker:
            (d / "root" / "run").mkdir(parents=True)
            (d / "root" / "run" / "docker.sock").write_text("")
        if fib:
            (d / "net" / "fib_trie").write_text(fib)

    proc_entry(1, "0::/init.scope\n")
    proc_entry(4242, "0::/lxc/101/ns/init.scope\n", docker=True, fib=FIB_TRIE)
    proc_entry(4300, "0::/lxc/101/ns/system.slice/docker.service\n")
    proc_entry(5000, "0::/lxc/102/ns/init.scope\n")  # CT bez Dockera
    proc_entry(6000, "12:pids:/lxc/103/ns\n", docker=True)  # cgroup v1, brak pliku conf
    (proc / "self").mkdir()

    found = endpoints.discover_lxc(proc, pve, probe=lambda p: p.exists())
    assert [(e.id, e.name, e.pid, e.ip) for e in found] == [
        ("ct-101", "nextcloud", 4242, "192.168.1.50"),
        ("ct-103", "CT 103", 6000, None),
    ]
    assert found[0].socket.endswith(str(Path("4242/root/run/docker.sock")))

    registry = endpoints.EndpointRegistry(True, lambda sock: f"klient:{sock}", proc=proc, pve_dir=pve)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    if not Path("/var/run/docker.sock").exists():
        assert all(e.id != "local" for e in registry.list(refresh=True))  # Proxmox bez Dockera na hoście
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    assert registry.list(refresh=True)[0].id == "local"
    with pytest.raises(ValueError):
        endpoints.check_id("ct-1;rm")

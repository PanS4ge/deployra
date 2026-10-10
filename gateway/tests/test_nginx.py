import threading
import time

from deployra_common.schemas import AccessClient, AccessList, AccessUser, ProxyState, Site, Stream
from deployra_gateway import nginx_driver
from deployra_gateway.nginx_ctl import NginxControl

UP = {"host": "10.66.0.5", "port": 20001}


def test_proxy_site_with_tls_hsts_and_access_list():
    acl = AccessList(
        id=3, users=[AccessUser(username="admin", htpasswd="$apr1$abc$def")],
        clients=[AccessClient(address="192.168.1.0/24")], satisfy_any=True,
    )  # fmt: skip
    site = Site(
        id=7, domains=["app.example.com"], upstream=UP, cert_id=2, ssl_forced=True, hsts=True,
        access_list_id=3, advanced="add_header X-Test 1;",
        locations=[{"path": "/api", "upstream": {"host": "10.66.0.6", "port": 8080, "path": "/v1"}}],
    )  # fmt: skip
    conf = nginx_driver.render_site(site, acl, cert_ready=True, ipv6=True)
    header, http_block, https_block = conf.split("\n\nserver {")
    assert header.startswith("# Deployra: proxy #7")
    # HTTP: tylko ACME + przekierowanie
    assert "include /etc/nginx/deployra/snippets/acme.conf;" in http_block
    assert "return 301 https://$host$request_uri;" in http_block and "proxy_pass" not in http_block
    # HTTPS
    assert "listen 443 ssl;" in https_block and "listen [::]:443 ssl;" in https_block and "http2 on;" in https_block
    assert "ssl_certificate /etc/deployra/certs/2/fullchain.pem;" in https_block
    assert 'Strict-Transport-Security "max-age=63072000" always;' in https_block
    assert "proxy_pass http://10.66.0.5:20001;" in https_block
    assert "proxy_pass http://10.66.0.6:8080/v1;" in https_block
    assert "auth_basic_user_file /etc/nginx/deployra/access/3.htpasswd;" in https_block
    assert "allow 192.168.1.0/24;" in https_block and "deny all;" in https_block and "satisfy any;" in https_block
    assert 'proxy_set_header Authorization "";' in https_block
    assert "proxy_set_header Connection $connection_upgrade;" in https_block
    assert "add_header X-Test 1;" in https_block


def test_cert_not_ready_serves_http_only():
    site = Site(id=1, domains=["a.example.com"], upstream=UP, cert_id=5, ssl_forced=True)
    conf = nginx_driver.render_site(site, None, cert_ready=False, ipv6=False)
    assert "443" not in conf and "return 301" not in conf and "proxy_pass http://10.66.0.5:20001;" in conf
    assert "[::]" not in conf


def test_redirect_dead_stream_and_full_render():
    state = ProxyState(
        sites=[
            Site(id=1, kind="redirect", domains=["old.example.com"], redirect={"domain": "new.example.com", "code": 301}),
            Site(id=2, kind="dead", domains=["gone.example.com"]),
            Site(id=3, domains=["off.example.com"], upstream=UP, enabled=False),
        ],
        streams=[Stream(id=4, listen_port=2201, upstream_host="10.66.0.5", upstream_port=2222, udp=True)],
        access_lists=[AccessList(id=9, users=[AccessUser(username="u", htpasswd="$apr1$x$y")])],
    )
    files = nginx_driver.render(state, lambda cid: False, ipv6=False)
    assert set(files) == {"http/00-default.conf", "http/site-1.conf", "http/site-2.conf", "stream/stream-4.conf", "access/9.htpasswd"}
    assert "return 301 $scheme://new.example.com$request_uri;" in files["http/site-1.conf"]
    assert "return 404;" in files["http/site-2.conf"]
    assert "listen 2201;" in files["stream/stream-4.conf"] and "listen 2201 udp;" in files["stream/stream-4.conf"]
    assert files["access/9.htpasswd"] == "u:$apr1$x$y\n"
    assert "ssl_reject_handshake on;" in files["http/00-default.conf"]


def _fake_reloader(ctl_dir, verdict, stop):
    while not stop.is_set():
        for req in ctl_dir.glob("*.request"):
            (ctl_dir / f"{req.stem}.result").write_text(verdict)
            req.unlink()
        time.sleep(0.05)


def test_apply_rolls_back_rejected_config(tmp_path):
    ctl = NginxControl(tmp_path, reload=True, timeout=5)
    (tmp_path / "ctl").mkdir()
    stop = threading.Event()
    thread = threading.Thread(target=_fake_reloader, args=(tmp_path / "ctl", "ok\n", stop), daemon=True)
    thread.start()
    try:
        assert ctl.apply({"http/site-1.conf": "server { dobry }"}).status == "applied"
    finally:
        stop.set()
        thread.join()

    stop = threading.Event()
    bad = "error\nnginx: [emerg] unknown directive \"zly\"\n"
    thread = threading.Thread(target=_fake_reloader, args=(tmp_path / "ctl", bad, stop), daemon=True)
    thread.start()
    try:
        result = ctl.apply({"http/site-1.conf": "zly;", "http/site-2.conf": "x"})
    finally:
        stop.set()
        thread.join()
    assert result.status == "rejected" and "unknown directive" in result.message
    assert (tmp_path / "http" / "site-1.conf").read_text() == "server { dobry }"
    assert not (tmp_path / "http" / "site-2.conf").exists()
    assert not list(tmp_path.glob(".backup-*")) and not list(tmp_path.glob(".staging-*"))


def test_apply_timeout_keeps_new_config(tmp_path):
    ctl = NginxControl(tmp_path, reload=True, timeout=0.3)
    result = ctl.apply({"http/site-1.conf": "nowy"})
    assert result.status == "pending" and (tmp_path / "http" / "site-1.conf").read_text() == "nowy"


def test_apply_rejects_path_escape(tmp_path):
    import pytest

    ctl = NginxControl(tmp_path, reload=False)
    with pytest.raises(ValueError):
        ctl.apply({"../etc/passwd": "x"})
    with pytest.raises(ValueError):
        ctl.apply({"http/../../x.conf": "x"})

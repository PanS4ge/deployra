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
    assert set(files) == {"http/00-bans.conf", "http/00-default.conf", "http/site-1.conf", "http/site-2.conf", "stream/stream-4.conf", "access/9.htpasswd"}
    assert "return 301 $scheme://new.example.com$request_uri;" in files["http/site-1.conf"]
    assert "return 404;" in files["http/site-2.conf"]
    assert "listen 2201;" in files["stream/stream-4.conf"] and "listen 2201 udp;" in files["stream/stream-4.conf"]
    assert files["access/9.htpasswd"] == "u:$apr1$x$y\n"
    assert "ssl_reject_handshake on;" in files["http/00-default.conf"]


def test_default_page_passthrough_and_cosmic():
    # 1. Cosmic default page
    state_cosmic = ProxyState(default_page="deployra")
    files_cosmic = nginx_driver.render(state_cosmic, lambda cid: False, ipv6=False)
    assert "orbit-spin" in files_cosmic["http/00-default.conf"]
    assert "Deployra · Deep Space" not in files_cosmic["http/00-default.conf"]
    assert "return 404" in files_cosmic["http/00-default.conf"]

    # 2. Passthrough to custom upstream app
    state_pass = ProxyState(default_page="passthrough", default_upstream={"scheme": "http", "host": "10.8.0.5", "port": 8080, "path": ""})
    files_pass = nginx_driver.render(state_pass, lambda cid: False, ipv6=False)
    assert "proxy_pass http://10.8.0.5:8080;" in files_pass["http/00-default.conf"]
    assert "include /etc/nginx/deployra/snippets/proxy.conf;" in files_pass["http/00-default.conf"]



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


PANEL = {"verify": "http://10.66.0.1:8080/auth/forward", "callback": "http://10.66.0.1:8080/auth/forward/callback",
         "login": "https://deploy.example.com/login"}


def test_deployra_login_renders_auth_request_and_callback():
    from deployra_common.schemas import PanelAuth

    acl = AccessList(id=4, deployra_login=True, satisfy_any=True, clients=[AccessClient(address="192.168.1.0/24")])
    site = Site(id=8, domains=["app.example.com"], upstream=UP, access_list_id=4,
                locations=[{"path": "/api", "upstream": {"host": "10.66.0.6", "port": 8080, "path": ""}}])  # fmt: skip
    conf = nginx_driver.render_site(site, acl, cert_ready=False, ipv6=False, auth=PanelAuth(**PANEL))
    assert conf.count("location = /__deployra_auth {") == 1  # wspólne lokalizacje raz na serwer
    assert "proxy_pass http://10.66.0.1:8080/auth/forward;" in conf
    assert "proxy_pass_request_body off;" in conf and 'proxy_set_header Content-Length "";' in conf
    assert "proxy_set_header X-Deployra-Host $host;" in conf and "proxy_set_header Cookie $http_cookie;" in conf
    assert "location = /__deployra_auth/callback {\n        auth_request off;" in conf
    assert "proxy_pass http://10.66.0.1:8080/auth/forward/callback;" in conf
    assert "location @deployra_login {" in conf
    assert "return 302 https://deploy.example.com/login?next=/auth/forward/start?rd=$scheme://$host$uri;" in conf
    assert conf.count("auth_request /__deployra_auth;") == 2  # „/” i „/api”
    assert conf.count("error_page 401 = @deployra_login;") == 2
    assert conf.count("proxy_set_header X-Deployra-User $deployra_user;") == 2
    assert "satisfy any;" in conf and "allow 192.168.1.0/24;" in conf and "deny all;" in conf


def test_deployra_login_all_mode_and_basic_users():
    from deployra_common.schemas import PanelAuth

    acl = AccessList(id=5, deployra_login=True, users=[AccessUser(username="u", htpasswd="$apr1$x$y")],
                     clients=[AccessClient(address="10.0.0.0/8")])  # fmt: skip
    site = Site(id=9, domains=["b.example.com"], upstream=UP, access_list_id=5)
    conf = nginx_driver.render_site(site, acl, cert_ready=False, ipv6=False, auth=PanelAuth(**PANEL))
    assert 'auth_basic "Dostep chroniony";' in conf and "satisfy all;" in conf
    assert "satisfy any;" not in conf and "auth_request /__deployra_auth;" in conf


def test_deployra_login_without_panel_address_fails_closed():
    acl = AccessList(id=6, deployra_login=True)
    site = Site(id=10, domains=["c.example.com"], upstream=UP, access_list_id=6)
    conf = nginx_driver.render_site(site, acl, cert_ready=False, ipv6=False, auth=None)
    assert "return 503;" in conf and "auth_request" not in conf and "__deployra_auth" not in conf


def test_plain_access_list_unchanged_and_redirect_site_protected():
    from deployra_common.schemas import PanelAuth

    acl = AccessList(id=7, users=[AccessUser(username="u", htpasswd="$apr1$x$y")])
    site = Site(id=11, domains=["d.example.com"], upstream=UP, access_list_id=7)
    conf = nginx_driver.render_site(site, acl, cert_ready=False, ipv6=False, auth=None)
    assert "auth_request" not in conf and "__deployra" not in conf and "return 503" not in conf
    # przekierowanie z logowaniem: lokalizacja przekierowania też chroniona, bez nagłówka X-Deployra-User
    red = Site(id=12, kind="redirect", domains=["r.example.com"], redirect={"domain": "new.example.com", "code": 301})
    conf = nginx_driver.render_site(red, AccessList(id=8, deployra_login=True), cert_ready=False, ipv6=False, auth=PanelAuth(**PANEL))
    assert "error_page 401 = @deployra_login;" in conf and "return 301 $scheme://new.example.com$request_uri;" in conf
    assert "X-Deployra-User" not in conf and "location = /__deployra_auth/callback" in conf


def test_render_passes_panel_auth_and_rejects_unsafe_urls():
    import pytest
    from pydantic import ValidationError

    from deployra_common.schemas import PanelAuth

    state = ProxyState(
        sites=[Site(id=1, domains=["e.example.com"], upstream=UP, access_list_id=2)],
        access_lists=[AccessList(id=2, deployra_login=True)],
        panel_auth=PanelAuth(**PANEL),
    )
    files = nginx_driver.render(state, lambda cid: False, ipv6=False)
    assert "proxy_pass http://10.66.0.1:8080/auth/forward;" in files["http/site-1.conf"]
    with pytest.raises(ValidationError):
        PanelAuth(**{**PANEL, "login": "https://x.example.com/login;}  server {"})
    with pytest.raises(ValidationError):
        PanelAuth(**{**PANEL, "verify": "http://10.0.0.1:8080/a b"})


def _h3_site(id: int = 5, domain: str = "h3.example.com", **kw):
    return Site(id=id, domains=[domain], upstream=UP, cert_id=2, http3=True, **kw)


def test_http3_listen_alt_svc_and_directive_in_tls_block():
    conf = nginx_driver.render_site(_h3_site(), None, cert_ready=True, ipv6=True, http3=True)
    http_block, https_block = conf.split("\n\nserver {")[1:]
    assert "quic" not in http_block and "Alt-Svc" not in http_block
    assert "listen 443 quic;" in https_block and "listen [::]:443 quic;" in https_block
    assert "http3 on;" in https_block
    assert "add_header Alt-Svc 'h3=\":443\"; ma=86400' always;" in https_block
    assert "reuseport" not in conf  # reuseport tylko raz — na serwerze domyślnym
    assert conf.count("add_header Alt-Svc") == 2  # blok serwera i lokacja „/” (add_header lokacji nadpisuje ten z serwera)


def test_http3_alt_svc_survives_caching_location():
    conf = nginx_driver.render_site(_h3_site(caching=True), None, cert_ready=True, ipv6=False, http3=True)
    location = conf[conf.index("location / {"):]
    assert "include /etc/nginx/deployra/snippets/cache.conf;" in location
    assert "add_header Alt-Svc 'h3=\":443\"; ma=86400' always;" in location
    assert "[::]" not in conf and "listen 443 quic;" in conf


def test_http3_skipped_without_module_option_or_certificate():
    for site, module, ready in (
        (_h3_site(), False, True),  # nginx bez modułu v3
        (Site(id=5, domains=["h3.example.com"], upstream=UP, cert_id=2), True, True),  # host bez opcji HTTP/3
        (_h3_site(), True, False),  # certyfikat jeszcze nie gotowy
    ):
        conf = nginx_driver.render_site(site, None, cert_ready=ready, ipv6=True, http3=module)
        assert "quic" not in conf and "http3" not in conf and "Alt-Svc" not in conf and "reuseport" not in conf


def test_http3_reuseport_exactly_once_on_default_server():
    state = ProxyState(sites=[_h3_site(id=5), _h3_site(id=6, domain="b.example.com")])
    files = nginx_driver.render(state, lambda cid: True, ipv6=True, http3=True)
    default = files["http/00-default.conf"]
    assert "listen 443 default_server quic reuseport;" in default and "listen [::]:443 default_server quic reuseport;" in default
    assert "ssl_reject_handshake on;" in default
    assert sum(body.count("reuseport") for body in files.values()) == 2  # po jednym na adres: 0.0.0.0:443 i [::]:443
    assert "listen 443 quic;" in files["http/site-5.conf"] and "listen 443 quic;" in files["http/site-6.conf"]


def test_http3_default_server_untouched_without_module_hosts_or_certificates():
    plain = ProxyState(sites=[Site(id=1, domains=["a.example.com"], upstream=UP, cert_id=2)])
    cases = (
        (ProxyState(sites=[_h3_site()]), lambda cid: True, False),  # brak modułu
        (plain, lambda cid: True, True),  # moduł jest, żaden host nie ma opcji
        (ProxyState(sites=[_h3_site()]), lambda cid: False, True),  # opcja jest, certyfikat nie
    )
    for state, ready, module in cases:
        files = nginx_driver.render(state, ready, ipv6=True, http3=module)
        assert not [name for name, body in files.items() if "quic" in body or "reuseport" in body or "Alt-Svc" in body]
        assert "listen 443 default_server ssl;" in files["http/00-default.conf"]


def test_http3_capability_probe_and_apply(ctx):
    assert ctx.http3_capable() is False and ctx.public_info()["http3"] is False
    base = ctx.config.nginx_dir / "base"
    base.mkdir(parents=True, exist_ok=True)
    (base / "http3").write_text("1\n", encoding="utf-8")
    assert ctx.http3_capable() is True and ctx.public_info()["http3"] is True

    certs = ctx.config.certs_dir / "2"
    certs.mkdir(parents=True, exist_ok=True)
    (certs / "fullchain.pem").write_text("x", encoding="utf-8")
    (certs / "privkey.pem").write_text("x", encoding="utf-8")
    result = ctx.apply_proxy(ProxyState(sites=[_h3_site()]))
    assert result.status == "written"
    site_conf = (ctx.config.nginx_dir / "http" / "site-5.conf").read_text(encoding="utf-8")
    assert "listen 443 quic;" in site_conf and "http3 on;" in site_conf
    default = (ctx.config.nginx_dir / "http" / "00-default.conf").read_text(encoding="utf-8")
    assert default.count("reuseport") == 2

    (base / "http3").write_text("0\n", encoding="utf-8")
    ctx.apply_proxy(ProxyState(sites=[_h3_site()]))
    site_conf = (ctx.config.nginx_dir / "http" / "site-5.conf").read_text(encoding="utf-8")
    default = (ctx.config.nginx_dir / "http" / "00-default.conf").read_text(encoding="utf-8")
    assert "quic" not in site_conf and "reuseport" not in default


def test_deployra_user_header_cleared_without_login():
    conf = nginx_driver.render_site(Site(id=9, domains=["plain.example.com"], upstream=UP), None, cert_ready=False, ipv6=False)
    assert 'proxy_set_header X-Deployra-User "";' in conf

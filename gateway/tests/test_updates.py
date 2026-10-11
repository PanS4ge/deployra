import time

from fastapi.testclient import TestClient

from deployra_gateway import nginx_ctl
from deployra_gateway.app import create_api_app, create_bootstrap_app

from test_gateway_api import _pair, _signed


def test_install_base_copies_bundle_once(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "nginx.conf").write_bytes(b"events {}\r\nhttp {}\r\n")
    (bundle / "deployra-reloader.sh").write_bytes(b"#!/bin/sh\n")
    target = tmp_path / "nginx"
    assert nginx_ctl.install_base(target, bundle) is True
    assert (target / "base" / "nginx.conf").read_bytes() == b"events {}\nhttp {}\n"  # bez CRLF
    assert nginx_ctl.install_base(target, bundle) is False
    (bundle / "nginx.conf").write_bytes(b"events {}\n")
    assert nginx_ctl.install_base(target, bundle) is True


def test_repo_bundle_is_complete(ctx):
    for name in nginx_ctl.BASE_FILES:
        assert (ctx.config.nginx_bundle / name).is_file()


def test_update_api(ctx, tmp_path):
    ctx.updates.dir = tmp_path / "upd"
    boot = TestClient(create_bootstrap_app(ctx))
    secret, _ = _pair(boot, ctx)
    api = TestClient(create_api_app(ctx, start_scheduler=False))
    status = _signed(api, secret, "GET", "/api/update").json()
    assert status["available"] is False and status["version"]
    assert _signed(api, secret, "POST", "/api/update", {"version": "1.2.0"}).status_code == 409

    (tmp_path / "upd").mkdir()
    (tmp_path / "upd" / "agent").write_text(f"mode=image\nupdated={int(time.time())}\n", encoding="utf-8")
    assert _signed(api, secret, "POST", "/api/update", {"version": "1.2.0; reboot"}).status_code == 400
    started = _signed(api, secret, "POST", "/api/update", {"version": "1.2.0"}).json()
    assert started["state"] == "queued" and started["target"] == "1.2.0"
    assert (tmp_path / "upd" / "request").exists()
    assert api.post("/api/update", json={"version": "1.2.0"}).status_code == 401  # bez podpisu

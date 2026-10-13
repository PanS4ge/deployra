import gzip
import time
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from deployra_common.schemas import ProxyState
from deployra_gateway import nginx_driver
from deployra_gateway.app import create_api_app, create_bootstrap_app, security_tick
from deployra_gateway.bans import normalize
from deployra_gateway.geoip import GeoIP
from deployra_gateway.ipwatch import IpWatch

from test_gateway_api import _pair, _signed

NOW = 1_760_000_000.0


def hit(watch, ip, ts=NOW, status=200, path="/", host="shop.example.com", size=100):
    watch.ingest({"a": ip, "s": status, "r": path, "h": host, "b": size, "t": ts}, ts)


def test_top_and_domain_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: NOW)
    watch = IpWatch(tmp_path)
    for _ in range(5):
        hit(watch, "1.1.1.1")
    hit(watch, "1.1.1.1", host="blog.example.com", status=404)
    for _ in range(3):
        hit(watch, "8.8.8.8", host="blog.example.com", status=503)
    top = watch.top()
    assert [item["ip"] for item in top] == ["1.1.1.1", "8.8.8.8"]
    assert top[0]["requests"] == 6 and top[0]["err4"] == 1 and top[0]["bytes"] == 600
    assert top[1]["err5"] == 3 and top[1]["domains"] == ["blog.example.com"]
    blog = watch.top(domain="blog.example.com")
    assert [(i["ip"], i["requests"]) for i in blog] == [("8.8.8.8", 3), ("1.1.1.1", 1)]

    watch.flush()  # po restarcie dzisiejsze dane wracają z bazy
    again = IpWatch(tmp_path)
    assert again.top()[0]["requests"] == 6


def test_suspicious_rate_scan_and_probes(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: NOW)
    watch = IpWatch(tmp_path)
    for _ in range(700):
        hit(watch, "203.0.113.9")  # zalew
    for i in range(40):
        hit(watch, "198.51.100.7", status=404, path=f"/page{i}")  # skaner: same 404
    for path in ("/.env", "/wp-login.php", "/.git/config"):
        hit(watch, "192.0.2.50", status=404, path=path)
    hit(watch, "1.1.1.1")
    found = {item["ip"]: item for item in watch.suspicious(rate_limit=600, now=NOW)}
    assert set(found) == {"203.0.113.9", "198.51.100.7", "192.0.2.50"}
    assert found["203.0.113.9"]["severity"] == "high" and found["203.0.113.9"]["reasons"][0] == {"code": "rate", "value": 700}
    assert {"code": "scan", "value": 100} in found["198.51.100.7"]["reasons"]
    assert found["192.0.2.50"]["reasons"] == [{"code": "probes", "value": 3}] and "/.env" in found["192.0.2.50"]["paths"]
    # 10 minut później okno jest puste
    assert watch.suspicious(rate_limit=600, now=NOW + 600) == []


def test_memory_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr("deployra_gateway.ipwatch.MINUTE_CAP", 50)
    monkeypatch.setattr("deployra_gateway.ipwatch.DAY_CAP", 50)
    watch = IpWatch(tmp_path)
    for i in range(1000):
        hit(watch, f"5.5.{i // 250}.{i % 250}", ts=time.time())
    assert len(watch.current) <= 101 and len(watch.today) <= 101


@pytest.mark.parametrize("target", ["10.0.0.1", "192.168.1.0/24", "127.0.0.1", "8.0.0.0/8", "fe80::1", "nonsense"])
def test_ban_targets_are_validated(target):
    with pytest.raises(ValueError):
        normalize(target)


def test_ban_normalization_and_protected():
    assert normalize("8.8.8.8/32") == "8.8.8.8" and normalize("8.8.4.0/24") == "8.8.4.0/24"
    with pytest.raises(ValueError):
        normalize("8.8.8.8", protected=["8.8.8.0/24"])


def test_nginx_renders_bans():
    files = nginx_driver.render(ProxyState(streams=[{"id": 1, "listen_port": 2222, "upstream_host": "10.66.0.5", "upstream_port": 22}],
                                           sites=[{"id": 1, "domains": ["a.example.com"], "upstream": {"host": "10.66.0.5", "port": 20000}}]),
                                lambda _: False, ipv6=False, bans=["8.8.8.8", "9.9.0.0/16"])  # fmt: skip
    assert "geo $deployra_ban {" in files["http/00-bans.conf"] and "    9.9.0.0/16 1;" in files["http/00-bans.conf"]
    assert sorted(files)[0] == "http/00-bans.conf"  # geo musi być zdefiniowane przed serwerami
    assert "if ($deployra_ban) {\n        return 444;" in files["http/site-1.conf"]
    assert "if ($deployra_ban)" in files["http/00-default.conf"]
    assert "deny 8.8.8.8;" in files["stream/stream-1.conf"]


def test_ban_api_and_auto_ban(ctx, monkeypatch):
    boot = TestClient(create_bootstrap_app(ctx))
    secret, _ = _pair(boot, ctx)
    api = TestClient(create_api_app(ctx, start_scheduler=False))
    assert _signed(api, secret, "POST", "/api/bans", {"target": "10.0.0.5"}).status_code == 400
    response = _signed(api, secret, "POST", "/api/bans", {"target": "8.8.8.8", "minutes": 30, "reason": "test"})
    assert response.status_code == 200 and response.json()["ban"]["target"] == "8.8.8.8"
    assert "8.8.8.8 1;" in (ctx.config.nginx_dir / "http" / "00-bans.conf").read_text()
    listed = _signed(api, secret, "GET", "/api/bans").json()
    assert listed["items"][0]["target"] == "8.8.8.8" and listed["firewall"] is False

    for _ in range(50):
        ctx.ipwatch.ingest({"a": "1.0.0.1", "s": 200, "h": "a.example.com", "t": time.time()})
    suspicious = _signed(api, secret, "GET", "/api/traffic/suspicious").json()
    assert suspicious["items"] == []  # 50/min to jeszcze nie zalew
    settings = _signed(api, secret, "PUT", "/api/bans/settings", {"auto": True, "rate": 60, "minutes": 5, "allowlist": []}).json()
    assert settings["rate"] == 60
    for _ in range(20):
        ctx.ipwatch.ingest({"a": "1.0.0.1", "s": 200, "h": "a.example.com", "t": time.time()})
    assert security_tick(ctx) is True
    assert ctx.bans.is_banned("1.0.0.1") and ctx.bans.list()[0]["auto"] is True
    top = _signed(api, secret, "GET", "/api/traffic/top").json()
    assert top["items"][0]["ip"] == "1.0.0.1" and top["items"][0]["banned"] is True

    assert _signed(api, secret, "POST", "/api/bans/remove", {"target": "8.8.8.8"}).status_code == 200
    assert _signed(api, secret, "POST", "/api/bans/remove", {"target": "8.8.8.8"}).status_code == 404
    ctx.state.mutate("bans", lambda bans: {k: {**v, "expires": 1} for k, v in bans.items()})
    assert ctx.bans.expire() is True and ctx.bans.nets() == []


class FakeReader:
    def __init__(self, data):
        self.data = data

    def get(self, ip):
        return self.data.get(ip)


def test_geoip_lookup_and_refresh(tmp_path):
    geo = GeoIP(tmp_path)
    geo._readers = {
        "country": FakeReader({"8.8.8.8": {"country": {"iso_code": "US", "names": {"en": "United States", "pl": "Stany Zjednoczone"}}}}),
        "asn": FakeReader({"8.8.8.8": {"autonomous_system_number": 15169, "autonomous_system_organization": "Google LLC"}}),
    }
    assert geo.lookup("8.8.8.8", "pl") == {"country": "US", "country_name": "Stany Zjednoczone", "asn": 15169, "isp": "Google LLC"}
    assert geo.lookup("192.168.1.1") == {"private": True}

    requested = []

    def handler(request):
        requested.append(request.url.path)
        if request.url.path.count("-") and request.url.path.endswith(datetime.now(timezone.utc).strftime("%Y-%m") + ".mmdb.gz"):
            return httpx.Response(404)  # bieżący miesiąc jeszcze niewydany → poprzedni
        return httpx.Response(200, content=gzip.compress(b"MMDB"))

    import asyncio

    updated = asyncio.run(geo.refresh(force=True, transport=httpx.MockTransport(handler)))
    assert updated == ["country", "asn"] and len(requested) == 4
    assert geo.path("country").read_bytes() == b"MMDB" and geo.status()["country"] is True


def test_cloudflare_ips_protected(ctx):
    # Cloudflare IP ranges are automatically allowed and cannot be banned
    assert ctx.bans.allowed("104.21.5.10") is True
    assert ctx.bans.allowed("172.67.100.20") is True
    with pytest.raises(ValueError):
        ctx.bans.add("104.16.1.1")


def test_sample_requests_captured(tmp_path):
    watch = IpWatch(tmp_path)
    watch.ingest({"a": "198.51.100.22", "m": "POST", "r": "/api/v1/auth?token=123", "s": 401, "h": "app.example.com", "b": 42, "u": "curl/7.68.0"})
    top = watch.top()
    assert len(top) == 1
    samples = top[0]["samples"]
    assert len(samples) == 1
    assert samples[0]["method"] == "POST"
    assert samples[0]["path"] == "/api/v1/auth?token=123"
    assert samples[0]["status"] == 401
    assert samples[0]["ua"] == "curl/7.68.0"


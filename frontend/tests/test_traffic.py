import json


class FakeGateway:
    def __init__(self):
        self.calls = []

    async def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        geo = {"country": "DE", "country_name": "Germany", "asn": 24940, "isp": "Hetzner Online GmbH"}
        if path == "/api/traffic/top":
            return {"items": [{"ip": "5.9.1.2", "requests": 120, "bytes": 2048, "err4": 3, "err5": 0, "probes": 2, "last": 1_760_000_000,
                               "paths": ["/.env"], "domains": ["shop.example.com"], "geo": geo, "banned": False, "allowlisted": False},
                              {"ip": "10.0.0.7", "requests": 5, "bytes": 1, "err4": 0, "err5": 0, "probes": 0, "last": 1_760_000_000,
                               "paths": [], "domains": [], "geo": {"private": True}, "banned": False, "allowlisted": False}],
                    "geoip": {"country": True}, "retention_days": 7}  # fmt: skip
        if path == "/api/traffic/suspicious":
            return {"items": [{"ip": "5.9.1.2", "reasons": [{"code": "rate", "value": 900}, {"code": "probes", "value": 3}], "severity": "high",
                               "requests": 4000, "peak": 900, "err4": 0, "paths": ["/.env"], "domains": [], "geo": geo,
                               "banned": False, "allowlisted": False}], "settings": {}}  # fmt: skip
        return {"items": [{"target": "1.2.3.0/24", "reason": "test", "created": 1_760_000_000, "expires": None, "auto": True, "geo": {}}],
                "settings": {"auto": True, "rate": 900, "minutes": 30, "allowlist": ["8.8.8.8"]}, "firewall": True,
                "geoip": {"country": True, "updated": 1_760_000_000}}  # fmt: skip

    async def post(self, path, json=None, **kwargs):
        self.calls.append(("POST", path, json))
        if path == "/api/bans":
            return {"ban": {"target": json["target"]}, "nginx": {"ok": True, "status": "applied"}}
        return {"ok": True}

    async def put(self, path, json=None, **kwargs):
        self.calls.append(("PUT", path, json))
        return json


def test_traffic_pages_with_gateway(admin, monkeypatch):
    panel = admin.app.state.panel
    fake = FakeGateway()
    monkeypatch.setattr(panel, "gateway_client", lambda: fake)
    monkeypatch.setattr(panel, "gateway", lambda: {"public_host": "vps.example.com"})

    page = admin.get("/traffic", headers={"HX-Request": ""}).text
    assert 'value="900"' in page and "8.8.8.8" in page and "IP Geolocation by DB-IP" in page

    top = admin.get("/traffic/top?days=7&domain=shop.example.com").text
    assert "\U0001F1E9\U0001F1EA Germany" in top and "Hetzner Online GmbH" in top and "5.9.1.2" in top
    assert top.count('hx-post="/traffic/ban"') == 1  # adresu prywatnego nie da się zbanować
    assert ("GET", "/api/traffic/top", {"days": 7, "domain": "shop.example.com", "limit": 50}) in fake.calls

    suspicious = admin.get("/traffic/suspicious").text
    assert "900 zapytań/min" in suspicious and "szukanie dziur: 3" in suspicious

    bans = admin.get("/traffic/bans").text
    assert "1.2.3.0/24" in bans and "na stałe" in bans

    response = admin.post("/traffic/ban", data={"target": " 9.9.9.9 ", "minutes": "60", "reason": "flood"})
    assert response.status_code == 200 and json.loads(response.headers["HX-Trigger"])["bans-changed"] is True
    assert ("POST", "/api/bans", {"target": "9.9.9.9", "minutes": 60, "reason": "flood"}) in fake.calls
    admin.post("/traffic/unban", data={"target": "9.9.9.9"})
    assert ("POST", "/api/bans/remove", {"target": "9.9.9.9"}) in fake.calls
    admin.post("/traffic/settings", data={"auto": "1", "rate": "1200", "minutes": "15", "allowlist": "1.1.1.1\n8.8.8.8"})
    assert ("PUT", "/api/bans/settings", {
        "auto": True, "rate": 1200, "minutes": 15, "err404_threshold": 15,
        "probe_threshold": 5, "ban_404": False, "ban_probes": False, "allowlist": ["1.1.1.1", "8.8.8.8"]
    }) in fake.calls


def test_traffic_requires_proxy_permission(admin):
    panel = admin.app.state.panel
    panel.db.execute("UPDATE users SET permissions = '1'")
    assert admin.get("/traffic", headers={"HX-Request": ""}).status_code == 403

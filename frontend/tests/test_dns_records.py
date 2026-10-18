import json

import httpx

from deployra_common.schemas import AppSpec
from deployra_panel.services import deploy, dns, dns_records, sync
from test_resources import nodes  # noqa: F401 — fixture `nodes`

ZONE = {"id": "z1", "name": "example.com"}


class FakeCloudflare:
    """Atrapa API Cloudflare: strefy i rekordy w pamięci."""

    def __init__(self):
        self.records = {
            "r1": {"id": "r1", "type": "A", "name": "blog.example.com", "content": "203.0.113.10", "proxied": False, "ttl": 1},
            "r2": {"id": "r2", "type": "A", "name": "nas.example.com", "content": "203.0.113.10", "proxied": True, "ttl": 300},
            "r3": {"id": "r3", "type": "TXT", "name": "example.com", "content": "v=spf1 -all", "ttl": 3600},
            "r4": {"id": "r4", "type": "CNAME", "name": "x.dev.example.com", "content": "example.com", "proxied": False, "ttl": 1},
        }
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path.removeprefix("/client/v4"), request.method
        self.calls.append((method, path))
        if path == "/zones":
            return self.ok([ZONE])
        record_id = path.rsplit("/", 1)[-1]
        if method == "GET":
            return self.ok(list(self.records.values()), {"total_pages": 1})
        if method == "POST":
            body = json.loads(request.content)
            self.records["new"] = {"id": "new", **body}
            return self.ok(self.records["new"])
        if method == "PATCH":
            self.records[record_id].update(json.loads(request.content))
            return self.ok(self.records[record_id])
        if method == "DELETE":
            self.records.pop(record_id)
            return self.ok({"id": record_id})
        return httpx.Response(404)

    @staticmethod
    def ok(result, info=None):
        return httpx.Response(200, json={"success": True, "result": result, "result_info": info or {}})


def test_dns_page_links_records_and_edits(admin, nodes, monkeypatch):
    panel = admin.app.state.panel
    cloudflare = FakeCloudflare()
    panel.db.set_setting("cloudflare_token", panel.sealer.seal("token"))
    panel.db.set_setting("public_ip", "203.0.113.10")
    monkeypatch.setattr(dns, "client", lambda p: dns.Cloudflare("token", transport=httpx.MockTransport(cloudflare.handler)))

    async def hub_status(panel):  # węzeł „dom” widziany przez WireGuard z adresu 203.0.113.10
        return {"peers": [{"id": "peer-dom", "endpoint": "203.0.113.10:51820"}]}

    monkeypatch.setattr(sync, "gateway_status", hub_status)
    panel.db.update("nodes", 1, peer_id="peer-dom")

    spec = AppSpec.model_validate({"slug": "blog", "source": {"kind": "image", "image": "ghost:5"}, "container_port": 2368, "domains": ["blog.example.com"]})
    app_id = deploy.create_app_row(panel, "Blog", 1, spec, {})
    site_blog = panel.db.insert("sites", kind="proxy", domains=["blog.example.com"], target={"type": "node", "node_id": 1, "port": 20001}, app_id=app_id)
    panel.db.update("apps", app_id, site_id=site_blog, host_port=20001)
    site_nas = panel.db.insert("sites", kind="proxy", domains=["*.dev.example.com", "nas.example.com"], target={"type": "address", "host": "10.8.0.12", "port": 443})
    panel.db.insert("dns_records", site_id=site_blog, domain="blog.example.com", zone_id="z1", record_id="r1")

    page = admin.get("/dns", headers={"HX-Request": ""}).text
    assert "example.com" in page and 'value="203.0.113.10"' in page
    table = admin.get("/dns/records", params={"zone": "z1", "name": "example.com"}).text
    assert "/apps/" in table and "/nodes/1/containers/app-blog" in table and "Deployra" in table  # rekord → aplikacja → kontener
    assert f"/proxy/hosts/{site_nas}" in table and "→ 10.8.0.12:443" in table  # nas i *.dev → host proxy z adresem
    assert table.count("dnsrec") == 0 and "dom · bramka</span>" in table and "1 h" in table and "5 min" in table  # IP rozpoznane: węzeł + bramka

    added = admin.post("/dns/records", data={"zone": "z1", "zone_name": "example.com", "type": "A", "name": "kuma", "content": "203.0.113.10",
                                             "ttl": "1", "proxied": "1"})  # fmt: skip
    assert added.status_code == 200 and "dns-changed" in added.headers["HX-Trigger"]
    assert cloudflare.records["new"]["name"] == "kuma.example.com" and cloudflare.records["new"]["proxied"] is True
    assert admin.post("/dns/records", data={"zone": "z1", "zone_name": "example.com", "type": "AAAA", "name": "x", "content": "1.2.3.4"}).status_code == 400

    edited = admin.post("/dns/records/r2", data={"zone": "z1", "zone_name": "example.com", "type": "A", "name": "nas.example.com",
                                                 "content": "203.0.113.20", "ttl": "60"})  # fmt: skip
    assert edited.status_code == 200 and cloudflare.records["r2"]["content"] == "203.0.113.20" and cloudflare.records["r2"]["proxied"] is False

    assert admin.post("/dns/records/r1/delete", data={"zone": "z1", "name": "blog.example.com"}).status_code == 200
    assert "r1" not in cloudflare.records and not panel.db.all("dns_records")


def test_record_name_and_ttl_helpers():
    assert dns_records.full_name("@", "example.com") == "example.com"
    assert dns_records.full_name("App", "example.com") == "app.example.com"
    assert dns_records.full_name("app.example.com.", "example.com") == "app.example.com"
    assert [dns_records.ttl_text(s) for s in (60, 300, 3600, 86400, 90)] == ["1 min", "5 min", "1 h", "1 d", "90 s"]


def test_dns_page_without_token(admin):
    assert "/settings/dns" in admin.get("/dns", headers={"HX-Request": ""}).text


def test_payload_srv_caa_and_validation():
    import pytest

    srv = dns_records.payload({"type": "SRV", "name": "mc", "service": "minecraft", "proto": "tcp", "port": "25566", "target": "mc.example.com",
                               "priority": "0", "weight": "5", "ttl": "300"}, "example.com")  # fmt: skip
    assert srv["name"] == "_minecraft._tcp.mc.example.com" and srv["data"] == {"priority": 0, "weight": 5, "port": 25566, "target": "mc.example.com"}
    edited = dns_records.payload({"name": "_minecraft._tcp.mc.example.com", "port": "25567", "target": "mc.example.com"}, "example.com", "SRV")
    assert edited["name"] == "_minecraft._tcp.mc.example.com" and edited["data"]["port"] == 25567  # edycja nie dokleja prefiksu drugi raz
    caa = dns_records.payload({"type": "CAA", "name": "@", "tag": "issue", "value": "letsencrypt.org"}, "example.com")
    assert caa == {"type": "CAA", "ttl": 1, "data": {"flags": 0, "tag": "issue", "value": "letsencrypt.org"}, "name": "example.com"}
    assert dns_records.payload({"type": "NS", "name": "dev", "content": "ns1.example.net."}, "example.com")["content"] == "ns1.example.net"
    for bad in ({"type": "SRV", "name": "mc", "service": "x y", "port": "1", "target": "a.b"}, {"type": "SRV", "name": "mc", "service": "mc", "port": "70000", "target": "a.b"},
                {"type": "CNAME", "name": "x", "content": "not a host!"}, {"type": "CAA", "name": "@", "tag": "evil", "value": "x"}, {"type": "HINFO", "name": "x", "content": "y"}):  # fmt: skip
        with pytest.raises(ValueError):
            dns_records.payload(bad, "example.com")
    assert dns_records.summary(srv) == "mc.example.com:25566"


def test_dns_templates_create_records(admin, monkeypatch):
    panel = admin.app.state.panel
    created = []

    def handler(request):
        if request.method == "POST":
            body = json.loads(request.content)
            created.append(body)
            if body["type"] == "TXT" and body["name"].startswith("_dmarc"):
                return httpx.Response(400, json={"success": False, "errors": [{"message": "record already exists"}]})
            return httpx.Response(200, json={"success": True, "result": {"id": str(len(created)), **body}})
        return httpx.Response(200, json={"success": True, "result": [ZONE], "result_info": {}})

    panel.db.set_setting("cloudflare_token", panel.sealer.seal("token"))
    monkeypatch.setattr(dns, "client", lambda p: dns.Cloudflare("token", transport=httpx.MockTransport(handler)))
    base = {"zone": "z1", "zone_name": "example.com"}

    ok = admin.post("/dns/templates", data={**base, "template": "minecraft", "name": "mc", "ip": "203.0.113.7", "port": "25570"})
    assert ok.status_code == 200 and "dns-changed" in ok.headers["HX-Trigger"]
    assert [(r["type"], r["name"]) for r in created] == [("A", "mc.example.com"), ("SRV", "_minecraft._tcp.mc.example.com")]
    assert created[0].get("proxied") is False and created[1]["data"]["port"] == 25570  # gra nie przejdzie przez proxy Cloudflare

    created.clear()
    web = admin.post("/dns/templates", data={**base, "template": "web", "name": "@", "ip": "203.0.113.7", "proxied": "1"})
    assert web.status_code == 200 and [(r["type"], r["name"], r.get("content")) for r in created] == [
        ("A", "example.com", "203.0.113.7"), ("CNAME", "www.example.com", "example.com")]
    created.clear()
    mail = admin.post("/dns/templates", data={**base, "template": "mail", "ip": "203.0.113.9"})
    assert mail.status_code == 200 and "nie udało" in json.loads(mail.headers["HX-Trigger"])["toast"]["message"]  # jeden rekord już był
    assert [r["type"] for r in created] == ["A", "MX", "TXT", "TXT"]

    created.clear()
    assert admin.post("/dns/templates", data={**base, "template": "minecraft", "name": "mc", "ip": "nie-ip"}).status_code == 400
    assert not created  # błąd walidacji — nic nie trafia do Cloudflare

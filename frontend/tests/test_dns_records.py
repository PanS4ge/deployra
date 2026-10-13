import json

import httpx

from deployra_common.schemas import AppSpec
from deployra_panel.services import deploy, dns, dns_records
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
    assert table.count("dnsrec") == 0 and ">bramka<" in table and "1 h" in table and "5 min" in table

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

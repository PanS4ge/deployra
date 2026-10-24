import asyncio
import json

import httpx
import pytest

from deployra_common.client import RemoteError
from deployra_common.jobs import JobFailed
from deployra_common.schemas import AppSpec
from deployra_panel.services import deploy, vulns

# PHP2Shell w bazie Bitnami (skrót prawdziwego wpisu OSV)
PHP2SHELL = {
    "id": "BIT-php-2026-109618", "summary": "RCE via OOM corruption", "modified": "2026-10-23T09:30:02Z", "published": "2026-10-22T08:08:19Z",
    "aliases": ["BIT-libphp-2026-109618", "CVE-2026-109618"], "database_specific": {"severity": "Critical"},
    "references": [{"type": "ADVISORY", "url": "https://github.com/php/php-src/security/advisories/GHSA-w8hw-w9fx-j79w"}],
    "affected": [{"package": {"name": "php", "ecosystem": "Bitnami"}, "ranges": [{"type": "SEMVER", "events": [
        {"introduced": "0"}, {"fixed": "8.2.35"}, {"introduced": "8.3.0"}, {"fixed": "8.3.36"}, {"introduced": "8.4.0"}, {"fixed": "8.4.27"},
        {"introduced": "8.5.0"}, {"fixed": "8.5.12"}]}],
        "severity": [{"type": "CVSS_V4", "score": "CVSS:4.0/AV:N/AC:H/AT:P/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N"}]}],
}  # fmt: skip
PHP_OLD = {
    "id": "BIT-php-2025-1217", "summary": "Header parser bug", "modified": "2025-03-01T00:00:00Z", "aliases": ["CVE-2025-1217"],
    "affected": [{"package": {"name": "php", "ecosystem": "Bitnami"}, "ranges": [{"type": "SEMVER", "events": [
        {"introduced": "8.3.0"}, {"fixed": "8.3.19"}]}], "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:N/I:L/A:N"}]}],
}  # fmt: skip
LARAVEL = {
    "id": "GHSA-5vg9-5847-vvmq", "summary": "CRLF injection", "modified": "2026-09-10T03:50:48Z", "aliases": ["CVE-2026-48019"],
    "database_specific": {"severity": "HIGH"},
    "affected": [{"package": {"name": "laravel/framework", "ecosystem": "Packagist"}, "ranges": [{"type": "ECOSYSTEM", "events": [
        {"introduced": "10.0.0"}, {"fixed": "10.48.29"}]}]}],
}  # fmt: skip
PYSEC_DUP = {  # ten sam CVE co GHSA, tylko z innej bazy — ma się nie dublować
    "id": "PYSEC-2026-1", "modified": "2026-09-10T00:00:00Z", "aliases": ["CVE-2026-48019"],
    "affected": [{"package": {"name": "laravel/framework", "ecosystem": "Packagist"}, "ranges": []}],
}  # fmt: skip


class Osv:
    """Atrapa OSV.dev i CISA KEV: `vulns` — (ekosystem, paczka, wersja) → lista wpisów."""

    def __init__(self):
        self.vulns = {("Bitnami", "php", "8.3.12"): [PHP2SHELL, PHP_OLD], ("Packagist", "laravel/framework", "10.0.0"): [LARAVEL, PYSEC_DUP]}
        self.kev = ["CVE-2026-109618"]
        self.details = 0
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("offline")
        if request.url.host == "www.cisa.gov":
            return httpx.Response(200, json={"vulnerabilities": [{"cveID": c} for c in self.kev]})
        if request.url.path == "/v1/querybatch":
            queries = json.loads(request.content)["queries"]
            return httpx.Response(200, json={"results": [
                {"vulns": [{"id": v["id"], "modified": v["modified"]} for v in self.vulns.get((q["package"]["ecosystem"], q["package"]["name"], q["version"]), [])]}
                for q in queries]})  # fmt: skip
        if request.url.path.startswith("/v1/vulns/"):
            self.details += 1
            vid = request.url.path.rsplit("/", 1)[1]
            record = next(v for vs in self.vulns.values() for v in vs if v["id"] == vid)
            return httpx.Response(200, json=record)
        return httpx.Response(404)

    def transport(self):
        return httpx.MockTransport(self.handler)


class Node:
    def __init__(self, php="8.3.12"):
        self.php, self.error, self.calls = php, None, []

    async def get(self, path, **_):
        self.calls.append(path)
        if path != "/api/security/inventory":
            raise httpx.ConnectError("offline")  # zasoby i reszta API węzła — tu niepotrzebne
        if self.error:
            raise self.error
        return {
            "endpoints": [{"id": "local", "name": "Host", "error": None, "containers": [
                {"id": "a1", "name": "app-sklep", "image": "deployra/rt-php:abc", "state": "running", "app": "sklep", "stack": None,
                 "software": [{"ecosystem": "Bitnami", "name": "php", "version": self.php, "source": "PHP_VERSION"}]},
                {"id": "b2", "name": "cache", "image": "redis:7-alpine", "state": "running", "app": None, "stack": None,
                 "software": [{"ecosystem": "Bitnami", "name": "redis", "version": "7.2.5", "source": "REDIS_VERSION"}]},
            ]}],
            "apps": {"sklep": [{"ecosystem": "Packagist", "name": "laravel/framework", "version": "10.0.0", "source": "composer.lock"}]},
        }  # fmt: skip


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch):
    monkeypatch.setattr(vulns, "RETRY_DELAY", 0)


@pytest.fixture
def setup(admin, monkeypatch):
    from deployra_panel.services import resources

    monkeypatch.setattr(resources, "LATEST", {})
    panel = admin.app.state.panel
    node_id = panel.db.insert("nodes", name="dom", transport="direct", api_url="http://127.0.0.1:1", status="paired", secret="x")
    spec = AppSpec.model_validate({"slug": "sklep", "source": {"kind": "runtime", "runtime": "php"}})
    app_id = deploy.create_app_row(panel, "Sklep", node_id, spec, {})
    node, osv = Node(), Osv()
    monkeypatch.setattr(panel, "node_client", lambda n: node)
    return panel, node, osv, app_id


def run(panel, osv):
    return asyncio.run(vulns.scan(panel, transport=osv.transport()))


def rows(panel, **where):
    found = panel.db.fetchall("SELECT * FROM vuln_findings ORDER BY id")
    return {r["package"]: r for r in found if all(r[k] == v for k, v in where.items())}


def test_version_order_and_fix_branch():
    assert vulns.vkey("8.4.0RC1") < vulns.vkey("8.4.0") == vulns.vkey("8.4") < vulns.vkey("8.4.1") < vulns.vkey("v10.0")
    record = vulns.compact(PHP2SHELL)
    assert vulns.fix_for(record, "Bitnami", "php", "8.3.12") == "8.3.36"
    assert vulns.fix_for(record, "Bitnami", "php", "8.4.1") == "8.4.27"
    assert vulns.fix_for(record, "Bitnami", "php", "8.1.30") == "8.2.35"  # gałąź bez wsparcia — poprawka dopiero w 8.2
    assert vulns.fix_for(record, "Bitnami", "php", "8.3.36") is None
    open_ended = {"affected": [{"ecosystem": "npm", "name": "x", "ranges": [[["introduced", "1.0.0"]]]}]}
    assert vulns.fix_for(open_ended, "npm", "x", "1.2.0") == ""


def test_severity_from_database_or_cvss():
    assert vulns.cvss3("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H") == 9.8
    assert vulns.cvss3("CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:C/C:H/I:H/A:L") == 8.9
    assert vulns.cvss3("CVSS:4.0/AV:N") is None
    assert vulns.rate(PHP2SHELL) == ("critical", None)  # Bitnami: waga w database_specific, wektor CVSS 4
    assert vulns.rate(PHP_OLD) == ("low", 3.7)
    assert vulns.rate(PYSEC_DUP) == ("unknown", None)


def test_scan_finds_php2shell_alerts_and_resolves_after_upgrade(setup):
    panel, node, osv, app_id = setup
    result = run(panel, osv)
    assert result["error"] is None and result["findings"] == 2 and result["nodes"][str(next(iter(panel.db.all("nodes")))["id"])]["error"] is None
    found = rows(panel)
    php, laravel = found["php"], found["laravel/framework"]
    assert set(found) == {"php", "laravel/framework"}  # redis bez podatności
    assert (php["severity"], php["kev"], php["fixed"], php["app_id"], php["container"]) == ("critical", 1, "8.3.36", app_id, "app-sklep")
    assert [v["cve"] for v in php["vulns"]] == ["CVE-2026-109618", "CVE-2025-1217"]  # wykorzystywana w atakach pierwsza
    assert (laravel["severity"], laravel["fixed"], laravel["container"], laravel["source"]) == ("high", "10.48.29", "", "composer.lock")
    assert len(laravel["vulns"]) == 1  # GHSA i PYSEC z tym samym CVE to jedna podatność
    alerts = panel.db.fetchall("SELECT * FROM alerts WHERE rule = 'vulnerability' AND status = 'firing'")
    assert {a["target_id"] for a in alerts} == {php["id"], laravel["id"]}
    assert "CVE-2026-109618" in next(a for a in alerts if a["target_id"] == php["id"])["message"]
    assert vulns.banner(panel) == {"high": 2, "show": True, "count": 1, "targets": 1, "kev": 1}

    details = osv.details
    run(panel, osv)  # ten sam stan: bez nowych alertów i bez ponownego pobierania szczegółów
    assert osv.details == details and len(panel.db.fetchall("SELECT * FROM alerts WHERE rule = 'vulnerability'")) == 2

    node.php = "8.3.36"
    run(panel, osv)
    assert rows(panel)["php"]["status"] == "resolved" and rows(panel)["laravel/framework"]["status"] == "open"
    assert panel.db.fetchone("SELECT status FROM alerts WHERE target_id = ? AND rule = 'vulnerability'", [php["id"]])["status"] == "resolved"
    assert vulns.banner(panel)["show"] is False


def test_same_package_in_two_versions_of_one_lockfile(setup):
    panel, node, osv, app_id = setup
    inventory = node.get

    async def two_versions(path, **kwargs):
        data = await inventory(path, **kwargs)
        data["apps"]["sklep"] = [{"ecosystem": "npm", "name": "lodash", "version": v, "source": "package-lock.json"} for v in ("4.17.20", "4.17.21")]
        return data

    node.get = two_versions
    lodash = {"id": "GHSA-lodash", "modified": "2026-01-01T00:00:00Z", "aliases": ["CVE-2021-23337"], "database_specific": {"severity": "HIGH"},
              "affected": [{"package": {"name": "lodash", "ecosystem": "npm"}, "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "4.17.21"}]}]}]}  # fmt: skip
    newer = {**lodash, "id": "GHSA-lodash-2", "aliases": ["CVE-2026-1"], "affected": [{"package": {"name": "lodash", "ecosystem": "npm"},
             "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "4.18.0"}]}]}]}  # fmt: skip
    osv.vulns[("npm", "lodash", "4.17.20")] = [lodash, newer]
    osv.vulns[("npm", "lodash", "4.17.21")] = [newer]
    run(panel, osv)
    found = sorted((r["version"], len(r["vulns"]), r["fixed"]) for r in panel.db.fetchall("SELECT * FROM vuln_findings WHERE package = 'lodash'"))
    assert found == [("4.17.20", 2, "4.18.0"), ("4.17.21", 1, "4.18.0")]


def test_unreachable_node_keeps_findings(setup):
    panel, node, osv, _ = setup
    run(panel, osv)
    node.error = RemoteError(404, "Not Found")
    result = run(panel, osv)
    entry = next(iter(result["nodes"].values()))
    assert "zaktualizuj" in entry["error"] or "update" in entry["error"]
    assert {r["status"] for r in rows(panel).values()} == {"open"}  # brak spisu to nie naprawa


def test_osv_down_is_reported(setup):
    panel, _, osv, _ = setup
    osv.down = True
    result = run(panel, osv)
    assert result["error"] and "OSV.dev" in result["error"] and vulns.status(panel)["error"] == result["error"]
    assert not rows(panel)


def test_transient_network_errors_are_retried(setup):
    panel, _, osv, _ = setup
    handler, failures = osv.handler, {"left": 2}

    def flaky(request):
        if request.url.path == "/v1/querybatch" and failures["left"]:
            failures["left"] -= 1
            raise httpx.ConnectError("getaddrinfo failed")
        return handler(request)

    osv.handler = flaky
    result = run(panel, osv)
    assert result["error"] is None and result["findings"] == 2


def test_failed_details_do_not_resolve_findings(setup):
    panel, _, osv, _ = setup
    run(panel, osv)
    panel.db.execute("DELETE FROM vuln_cache")  # wpisy trzeba pobrać jeszcze raz…
    handler = osv.handler

    def no_details(request):
        if request.url.path.startswith("/v1/vulns/"):
            raise httpx.ConnectError("getaddrinfo failed")  # …a OSV oddaje tylko listę ID
        return handler(request)

    osv.handler = no_details
    run(panel, osv)
    found = rows(panel)
    assert {r["status"] for r in found.values()} == {"open"}
    assert {v["severity"] for v in found["php"]["vulns"]} == {"unknown"} and found["php"]["fixed"] is None


def test_ignore_restore_and_reopen(setup, admin):
    panel, _, osv, _ = setup
    run(panel, osv)
    laravel = rows(panel)["laravel/framework"]
    assert admin.post(f"/security/{laravel['id']}/ignore").status_code == 204
    assert rows(panel)["laravel/framework"]["status"] == "ignored"
    assert panel.db.fetchone("SELECT status FROM alerts WHERE target_id = ? AND rule = 'vulnerability'", [laravel["id"]])["status"] == "resolved"
    run(panel, osv)
    assert rows(panel)["laravel/framework"]["status"] == "ignored"
    newer = {**LARAVEL, "id": "GHSA-new0-0000-0000", "aliases": ["CVE-2026-50000"]}
    osv.vulns[("Packagist", "laravel/framework", "10.0.0")].append(newer)
    run(panel, osv)
    reopened = rows(panel)["laravel/framework"]
    assert reopened["status"] == "open" and len(reopened["vulns"]) == 2
    assert panel.db.fetchone("SELECT COUNT(*) AS n FROM alerts WHERE target_id = ? AND status = 'firing'", [laravel["id"]])["n"] == 1
    assert admin.post(f"/security/{laravel['id']}/ignore").status_code == 204
    assert admin.post(f"/security/{laravel['id']}/restore").status_code == 204
    assert rows(panel)["laravel/framework"]["status"] == "open"


def test_alert_threshold_setting(setup, admin):
    panel, _, osv, _ = setup
    assert admin.post("/security/settings", data={"enabled": "1", "interval_hours": "12", "alert_severity": "critical"}).status_code == 200
    assert vulns.settings(panel) == {"enabled": True, "interval_hours": 12, "alert_severity": "critical"}
    assert admin.post("/security/settings", data={"interval_hours": "7", "alert_severity": "high"}).status_code == 400
    run(panel, osv)
    firing = panel.db.fetchall("SELECT target_name FROM alerts WHERE rule = 'vulnerability' AND status = 'firing'")
    assert [a["target_name"] for a in firing] == ["Sklep · php 8.3.12"]  # wysoka (laravel) poniżej progu


def test_pages_show_findings_banner_and_app_callout(setup, admin):
    panel, _, osv, app_id = setup
    run(panel, osv)
    html = admin.get("/security", headers={"HX-Request": ""}).text
    assert "CVE-2026-109618" in html and "8.3.36" in html and "laravel/framework" in html and f"/security/" in html
    assert 'class="nav-count">2<' in html
    home = admin.get("/apps", headers={"HX-Request": ""}).text
    assert 'id="vuln-banner"' in home
    assert admin.post("/security/snooze").status_code == 200
    assert 'id="vuln-banner"' not in admin.get("/apps", headers={"HX-Request": ""}).text
    detail = admin.get(f"/apps/{app_id}", headers={"HX-Request": ""}).text
    assert "php 8.3.12" in detail and "8.3.36" in detail
    assert "laravel/framework" in admin.get("/security?show=open", headers={"HX-Request": ""}).text


def test_fix_redeploys_with_fresh_image_and_rescans(setup, admin, monkeypatch):
    panel, _, osv, app_id = setup
    run(panel, osv)
    calls = []

    async def fake_run(panel_, job, app, adopt_container=None, refresh=False):
        calls.append((app, refresh))
        return {}

    async def fake_scan(panel_, job=None, transport=None):
        calls.append("scan")
        return {}

    from deployra_panel.routes import security

    monkeypatch.setattr(security.deploy, "run", fake_run)
    monkeypatch.setattr(security.vulns, "scan", fake_scan)
    php = rows(panel)["php"]
    response = admin.post(f"/security/{php['id']}/fix")
    assert response.status_code == 204 and response.headers["HX-Redirect"].startswith(f"/apps/{app_id}?job=")
    job = panel.jobs.get(response.headers["HX-Redirect"].split("job=")[1])
    for _ in range(100):
        if job.done:
            break
        asyncio.run(asyncio.sleep(0.02))
    assert calls == [(app_id, True), "scan"]


def test_fix_refuses_plain_container(setup, admin):
    panel, _, osv, _ = setup
    osv.vulns[("Bitnami", "redis", "7.2.5")] = [{**PHP_OLD, "id": "BIT-redis-1", "affected": [{"package": {"name": "redis", "ecosystem": "Bitnami"},
                                                                                              "ranges": []}]}]  # fmt: skip
    run(panel, osv)
    redis = rows(panel)["redis"]
    assert redis["app_id"] is None and redis["label"] == "cache"
    assert admin.post(f"/security/{redis['id']}/fix").status_code == 400


def test_deploy_run_passes_refresh_to_node(setup, monkeypatch):
    panel, node, _, app_id = setup
    sent = []

    async def follow(client, path, body, job, prefix=""):
        sent.append(body)
        raise JobFailed("stop")

    monkeypatch.setattr(deploy.relay, "start_and_follow", follow)

    class Job:
        id = "j"

        def log(self, line):
            pass

    for refresh in (False, True):
        with pytest.raises(JobFailed):
            asyncio.run(deploy.run(panel, Job(), app_id, refresh=refresh))
    assert [b["refresh_image"] for b in sent] == [False, True]
    assert deploy.load(panel, panel.db.get("apps", app_id))[0].refresh_image is False  # nie zostaje w zapisanej specyfikacji

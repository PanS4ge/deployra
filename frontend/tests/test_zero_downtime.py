"""Wdrożenie bez przestojów po stronie panelu: bramka przełącza się na nowy port po teście węzła,
potem węzeł gasi starą wersję; błąd przed przełączeniem cofa port i cel bramki."""

import pytest

from deployra_common.jobs import JobFailed
from deployra_common.schemas import AppSpec
from deployra_panel.services import deploy, proxy_state, relay
from test_resources import job_of, nodes, wait_job  # noqa: F401 — fixture `nodes`

NEW_PORT = 20051  # port nowej wersji na węźle „dom” (stara wersja: 20001)


@pytest.fixture
def events(nodes, monkeypatch):
    """Wywołania węzła w kolejności; `fail` zawiera nazwy kroków, które mają się nie udać."""
    state = {"log": [], "fail": set()}

    async def follow(client, path, body, job, prefix=""):
        client.calls.append(("JOB", path, body))
        state["log"].append(path)
        if path.endswith("/handover/commit") and "commit" in state["fail"]:
            raise JobFailed("węzeł nie przełączył")
        if path == "/api/apps/deploy" and body.get("zero_downtime"):
            return {"result": {"host_port": client.port + 50, "upstream_host": None, "handover": True}}
        if path == "/api/apps/deploy":
            return {"result": {"host_port": client.port}}
        return {"result": {}}

    monkeypatch.setattr(relay, "start_and_follow", follow)
    return state


@pytest.fixture
def gateway(admin, events, monkeypatch):
    """Bramka: zapisuje cel każdego wypchnięcia (w kolejności z wywołaniami węzła); `reject` odrzuca konfigurację."""
    panel = admin.app.state.panel
    state = {"reject": False, "targets": []}

    async def push(_panel):
        events["log"].append("push")
        state["targets"].append(sorted(s["target"]["port"] for s in panel.db.all("sites")))
        if state["reject"]:
            return {"status": "rejected", "message": "zła konfiguracja"}
        return {"status": "applied"}

    monkeypatch.setattr(proxy_state, "push", push)
    return state


def make_app(admin, domains=("z.example.com",), zero=True):
    panel = admin.app.state.panel
    spec = AppSpec.model_validate({"slug": "web", "source": {"kind": "image", "image": "nginx:1.27"}, "container_port": 80,
                                   "domains": list(domains), "zero_downtime": zero, "health_path": "/up"})  # fmt: skip
    return deploy.create_app_row(panel, "Web", 1, spec, {})


def run_deploy(admin, app_id):
    panel = admin.app.state.panel
    return wait_job(panel, job_of(admin.post(f"/apps/{app_id}/deploy")))


def test_switch_after_gateway_then_commit(admin, nodes, events, gateway):
    app_id = make_app(admin)
    job = run_deploy(admin, app_id)
    assert job.status == "ok", (job.error, job.lines)
    app = admin.app.state.panel.db.get("apps", app_id)
    assert events["log"] == ["/api/apps/deploy", "push", "/api/apps/web/handover/commit"], events["log"]
    assert gateway["targets"] == [[NEW_PORT]]  # bramka dostała nowy port, zanim węzeł zgasił starą wersję
    assert app["host_port"] == NEW_PORT and app["status"] == "running"
    assert admin.app.state.panel.db.get("sites", app["site_id"])["target"]["port"] == NEW_PORT


def test_rejected_gateway_restores_old_port_and_aborts(admin, nodes, events, gateway):
    panel = admin.app.state.panel
    app_id = make_app(admin)
    # działająca wersja na starym porcie, z już wystawioną domeną
    deploy._upsert_site(panel, panel.db.get("apps", app_id), panel.node(1), 20001, {})
    panel.db.update("apps", app_id, host_port=20001, status="running")
    gateway["reject"] = True

    job = run_deploy(admin, app_id)
    assert job.status == "error"
    assert events["log"] == ["/api/apps/deploy", "push", "/api/apps/web/handover/abort"], events["log"]
    after = panel.db.get("apps", app_id)
    assert after["host_port"] == 20001 and after["status"] == "error"
    assert panel.db.get("sites", after["site_id"])["target"]["port"] == 20001  # cel bramki wrócił do starej wersji


def test_failure_after_switch_keeps_new_version(admin, nodes, events, gateway):
    panel = admin.app.state.panel
    app_id = make_app(admin)
    events["fail"].add("commit")

    job = run_deploy(admin, app_id)
    assert job.status == "error"
    assert events["log"] == ["/api/apps/deploy", "push", "/api/apps/web/handover/commit"], events["log"]  # bez wycofania
    assert panel.db.get("sites", panel.db.get("apps", app_id)["site_id"])["target"]["port"] == NEW_PORT


def test_app_without_domains_commits_right_away(admin, nodes, events, gateway):
    app_id = make_app(admin, domains=())
    job = run_deploy(admin, app_id)
    assert job.status == "ok", (job.error, job.lines)
    assert events["log"] == ["/api/apps/deploy", "/api/apps/web/handover/commit"] and gateway["targets"] == []
    assert admin.app.state.panel.db.get("apps", app_id)["host_port"] == NEW_PORT


def test_switch_off_is_plain_deploy(admin, nodes, events, gateway):
    app_id = make_app(admin, zero=False)
    job = run_deploy(admin, app_id)
    assert job.status == "ok", (job.error, job.lines)
    assert events["log"] == ["/api/apps/deploy", "push"]  # bez przełączenia i bez zatwierdzenia na węźle
    assert admin.app.state.panel.db.get("apps", app_id)["host_port"] == 20001


def test_settings_persist_in_columns_and_do_not_ask_to_redeploy(admin, nodes, events, gateway):
    panel = admin.app.state.panel
    app_id = make_app(admin, zero=False)
    form = {"name": "Web", "image": "nginx:1.27", "container_port": "80", "domains": "z.example.com",
            "zero_downtime": "1", "health_path": "/up"}  # fmt: skip
    response = admin.post(f"/apps/{app_id}/edit", data=form)
    assert response.status_code == 204 and "job=" not in response.headers["HX-Redirect"]  # sam sposób wdrażania nie pyta
    app = panel.db.get("apps", app_id)
    assert app["zero_downtime"] == 1 and app["health_path"] == "/up"
    assert deploy.load(panel, app)[0].zero_downtime is True


def test_toggle_shown_only_for_container_kinds(admin, nodes):
    panel = admin.app.state.panel
    image_id = make_app(admin, zero=False)
    compose = AppSpec.model_validate({"slug": "stack", "source": {"kind": "compose", "compose": "services:\n  web:\n    image: nginx\n"},
                                      "container_port": 80})  # fmt: skip
    compose_id = deploy.create_app_row(panel, "Stack", 1, compose, {})
    assert 'name="zero_downtime"' in admin.get(f"/apps/{image_id}/edit", headers={"HX-Request": ""}).text
    page = admin.get(f"/apps/{compose_id}/edit", headers={"HX-Request": ""}).text
    assert 'name="zero_downtime"' not in page and 'name="health_path"' not in page

from deployra_common.schemas import AppSpec
from deployra_panel.services import deploy
from test_resources import job_of, nodes, wait_job  # noqa: F401 — fixture `nodes`


def edit(admin, app_id, **fields):
    form = {"name": "Web", "image": "nginx", "container_port": "80", "domains": "", **fields}
    return admin.post(f"/apps/{app_id}/edit", data=form)


def test_domain_change_skips_container(admin, nodes):
    panel = admin.app.state.panel
    spec = AppSpec.model_validate({"slug": "web", "source": {"kind": "image", "image": "nginx"}, "container_port": 80,
                                   "domains": ["a.example.com"]})  # fmt: skip
    app_id = deploy.create_app_row(panel, "Web", 1, spec, {})
    assert wait_job(panel, job_of(admin.post(f"/apps/{app_id}/deploy"))).status == "ok"
    deploys = lambda: nodes[1].jobs().count("/api/apps/deploy")  # noqa: E731
    assert deploys() == 1

    # nowa domena → tylko bramka, kontener zostaje, bez pytania
    job = wait_job(panel, job_of(edit(admin, app_id, domains="b.example.com")))
    assert job.status == "ok" and deploys() == 1
    app = panel.db.get("apps", app_id)
    assert panel.db.get("sites", app["site_id"])["domains"] == ["b.example.com"]

    # bez domen → host proxy znika
    job = wait_job(panel, job_of(edit(admin, app_id)))
    assert job.status == "ok" and deploys() == 1
    assert panel.db.get("apps", app_id)["site_id"] is None and not panel.db.all("sites")

    # nic się nie zmieniło → „Zapisano”, bez wdrożenia
    assert "job=" not in edit(admin, app_id).headers["HX-Redirect"] and deploys() == 1


def test_container_change_asks_first(admin, nodes):
    panel = admin.app.state.panel
    spec = AppSpec.model_validate({"slug": "web", "source": {"kind": "image", "image": "nginx"}, "container_port": 80})
    app_id = deploy.create_app_row(panel, "Web", 1, spec, {})
    assert wait_job(panel, job_of(admin.post(f"/apps/{app_id}/deploy"))).status == "ok"
    deploys = lambda: nodes[1].jobs().count("/api/apps/deploy")  # noqa: E731

    # zmiana w kontenerze → pytanie z listą zmian, nic jeszcze nie zapisane
    ask = edit(admin, app_id, memory_mb="256", env="A=1", domains="c.example.com")
    assert ask.status_code == 200 and ask.headers["HX-Retarget"] == "#edit-confirm"
    assert "limit RAM" in ask.text and "zmienne środowiskowe" in ask.text and 'value="deploy"' in ask.text and "Postaw ponownie" in ask.text
    assert deploy.load(panel, panel.db.get("apps", app_id))[0].memory_mb is None and deploys() == 1

    # „Tylko zapisz” → zapisane, bez wdrożenia
    saved = edit(admin, app_id, memory_mb="256", env="A=1", domains="c.example.com", decision="save")
    assert saved.status_code == 204 and "job=" not in saved.headers["HX-Redirect"] and deploys() == 1
    assert deploy.load(panel, panel.db.get("apps", app_id))[0].memory_mb == 256

    # „Postaw ponownie” → wdrożenie (razem z domeną)
    job = wait_job(panel, job_of(edit(admin, app_id, memory_mb="512", env="A=1", domains="c.example.com", decision="deploy")))
    assert job.status == "ok" and deploys() == 2
    assert panel.db.get("sites", panel.db.get("apps", app_id)["site_id"])["domains"] == ["c.example.com"]

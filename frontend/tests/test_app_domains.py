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

    # nowa domena (nawet przez „Zapisz i wdróż”) → tylko bramka, kontener zostaje
    job = wait_job(panel, job_of(edit(admin, app_id, domains="b.example.com", deploy="1")))
    assert job.status == "ok" and deploys() == 1
    app = panel.db.get("apps", app_id)
    assert panel.db.get("sites", app["site_id"])["domains"] == ["b.example.com"]

    # bez domen → host proxy znika
    job = wait_job(panel, job_of(edit(admin, app_id, deploy="1")))
    assert job.status == "ok" and deploys() == 1
    assert panel.db.get("apps", app_id)["site_id"] is None and not panel.db.all("sites")

    # nic się nie zmieniło, ale „Zapisz i wdróż” → zwykłe wdrożenie (np. nowszy obraz :latest)
    assert wait_job(panel, job_of(edit(admin, app_id, deploy="1"))).status == "ok" and deploys() == 2
    # zmiana kontenera → wdrożenie
    assert wait_job(panel, job_of(edit(admin, app_id, memory_mb="256", domains="c.example.com", deploy="1"))).status == "ok"
    assert deploys() == 3 and panel.db.get("sites", panel.db.get("apps", app_id)["site_id"])["domains"] == ["c.example.com"]

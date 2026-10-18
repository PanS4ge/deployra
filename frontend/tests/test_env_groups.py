"""Zmienne współdzielone i edytor zmiennych: zestawy (CRUD), kolejność dołączania, scalanie przy wdrożeniu, edytor .env."""

import html
import re

import pytest

from deployra_common import envfile
from deployra_common.schemas import AppSpec
from deployra_panel.services import deploy, env_groups
from test_app_settings_roundtrip import EditForm  # noqa: F401 — ten sam sposób wysyłania formularza co w teście ustawień
from test_resources import job_of, nodes, wait_job  # noqa: F401 — fixture `nodes`


def make_app(panel, slug="web", env=None, scopes=None):
    spec = AppSpec.model_validate({
        "slug": slug, "source": {"kind": "image", "image": "nginx:1"}, "container_port": 80,
        "env": env or {}, "env_scopes": scopes or {},
    })  # fmt: skip
    app_id = deploy.create_app_row(panel, slug, 1, spec, {})
    panel.db.update("apps", app_id, status="running")
    return app_id


def test_group_crud_encrypts_values_and_reports_usage(admin, nodes):
    panel = admin.app.state.panel
    group_id = env_groups.save(panel, None, " Baza ", {"DB_HOST": "db", "DB_PASS": "tajne-haslo"}, {"DB_PASS": "build"})
    group = env_groups.get(panel, group_id)
    assert group == {"id": group_id, "name": "Baza", "env": {"DB_HOST": "db", "DB_PASS": "tajne-haslo"}, "scopes": {"DB_PASS": "build"}}
    assert "tajne-haslo" not in panel.db.get("env_groups", group_id)["payload"]  # w bazie tylko zaszyfrowany zapis

    with pytest.raises(ValueError):
        env_groups.save(panel, None, "Baza", {}, {})  # nazwa zajęta
    with pytest.raises(ValueError):
        env_groups.save(panel, None, "   ", {}, {})

    app_id = make_app(panel)
    env_groups.set_attached(panel, app_id, [group_id])
    assert env_groups.list_groups(panel) == [{"id": group_id, "name": "Baza", "count": 2, "apps": ["web"]}]

    env_groups.delete(panel, group_id)
    assert env_groups.list_groups(panel) == [] and env_groups.attached_ids(panel, app_id) == []


def test_attach_order_and_merge_group_layers_then_app(admin, nodes):
    panel = admin.app.state.panel
    general = env_groups.save(panel, None, "Ogólne", {"A": "1", "TOKEN": "t", "X": "ogolne"}, {"TOKEN": "build"})
    override = env_groups.save(panel, None, "Nadpisanie", {"X": "nadpisanie", "Y": "2"}, {})
    app_id = make_app(panel, env={"Y": "aplikacja", "Z": "3"}, scopes={"Z": "both"})

    env_groups.set_attached(panel, app_id, [override, general])
    assert env_groups.attached_ids(panel, app_id) == [override, general]
    with pytest.raises(ValueError):  # nieistniejący zestaw nie kasuje dotychczasowych przypisań
        env_groups.set_attached(panel, app_id, [general, 9999])
    assert env_groups.attached_ids(panel, app_id) == [override, general]

    spec, _ = deploy.load(panel, panel.db.get("apps", app_id))
    merged = env_groups.effective(panel, app_id, spec)
    # kolejność: nadpisanie, potem ogólne (ono nadpisuje X), na końcu zmienne aplikacji
    assert merged.env == {"X": "ogolne", "Y": "aplikacja", "A": "1", "TOKEN": "t", "Z": "3"}
    assert merged.env_scopes == {"TOKEN": "build", "Z": "both"}
    assert merged.build_env() == {"TOKEN": "t", "Z": "3"} and "TOKEN" not in merged.runtime_env()
    assert spec.env == {"Y": "aplikacja", "Z": "3"}  # zapisana specyfikacja aplikacji bez zmian

    panel.db.update("apps", app_id, status="running")
    job = wait_job(panel, job_of(admin.post(f"/apps/{app_id}/deploy")))
    assert job.status == "ok", job.error
    sent = [c for c in nodes[1].calls if c[0] == "JOB" and c[1] == "/api/apps/deploy"]
    assert sent and sent[-1][2]["env"] == merged.env and sent[-1][2]["env_scopes"] == merged.env_scopes


def test_edit_form_attaches_groups_in_displayed_order(admin, nodes):
    panel = admin.app.state.panel
    first = env_groups.save(panel, None, "Pierwszy", {"P": "1"}, {})
    second = env_groups.save(panel, None, "Drugi", {"Q": "2"}, {})
    app_id = make_app(panel, slug="formularz")
    page = admin.get(f"/apps/{app_id}/edit", headers={"HX-Request": ""})
    assert page.status_code == 200 and 'name="env_group"' in page.text
    form = EditForm(page.text).data
    form["env_group"] = [str(second), str(first)]
    response = admin.post(f"/apps/{app_id}/edit", data=form)
    assert response.status_code == 204 and "job=" not in response.headers["HX-Redirect"], response.headers
    assert env_groups.attached_ids(panel, app_id) == [second, first]


def test_editor_switches_between_fields_and_env_text(admin):
    fields = {"env_mode": "form", "env_key": ["A", "B"], "env_value": ["1", "linia1\nlinia2"], "env_scope": ["runtime", "build"]}
    raw = admin.post("/apps/env-editor?id=env&to=raw", data=fields)
    assert raw.status_code == 200 and 'name="env_raw"' in raw.text
    text = html.unescape(re.search(r'name="env_raw"[^>]*>(.*?)</textarea>', raw.text, re.S).group(1))
    assert envfile.parse(text) == ({"A": "1", "B": "linia1\nlinia2"}, {"B": "build"})

    back = admin.post("/apps/env-editor?id=env&to=form", data={"env_mode": "raw", "env_raw": text})
    assert back.status_code == 200 and 'name="env_key" value="B"' in back.text
    assert re.search(r'name="env_scope" :value="value" value="build"', back.text)

    bad = admin.post("/apps/env-editor?id=env&to=form", data={"env_mode": "raw", "env_raw": "BEZ_ZNAKU"})
    assert bad.status_code == 400


def test_editor_adds_and_removes_rows(admin):
    rows = {"env_mode": "form", "env_key": ["A", "B"], "env_value": ["1", "2"], "env_scope": ["runtime", "runtime"]}
    added = admin.post("/apps/env-editor?id=env&add=1", data=rows)
    assert added.text.count('name="env_key"') == 3
    removed = admin.post("/apps/env-editor?id=env&drop=0", data=rows)
    assert removed.text.count('name="env_key"') == 1 and 'name="env_key" value="B"' in removed.text


def test_settings_group_form_creates_updates_and_deletes(admin):
    panel = admin.app.state.panel
    created = admin.post("/settings/env-groups", data={
        "name": "SMTP", "env_mode": "form", "env_key": ["HOST"], "env_value": ["smtp.example.com"], "env_scope": ["runtime"],
    })  # fmt: skip
    assert created.status_code == 204 and "/settings/env-groups" in created.headers["HX-Redirect"]
    group_id = next(g["id"] for g in env_groups.list_groups(panel) if g["name"] == "SMTP")
    assert env_groups.get(panel, group_id)["env"] == {"HOST": "smtp.example.com"}

    assert admin.post("/settings/env-groups", data={"name": "SMTP", "env_mode": "form"}).status_code == 400  # nazwa zajęta

    updated = admin.post(f"/settings/env-groups/{group_id}", data={
        "name": "Poczta", "env_mode": "form", "env_key": ["HOST", "PORT"], "env_value": ["x", "587"], "env_scope": ["runtime", "build"],
    })  # fmt: skip
    assert updated.status_code == 204
    group = env_groups.get(panel, group_id)
    assert group["name"] == "Poczta" and group["env"] == {"HOST": "x", "PORT": "587"} and group["scopes"] == {"PORT": "build"}

    assert admin.post(f"/settings/env-groups/{group_id}/delete").status_code == 204
    assert env_groups.get(panel, group_id) is None

    page = admin.get("/settings/env-groups", headers={"HX-Request": ""})
    assert page.status_code == 200 and "Poczta" not in page.text

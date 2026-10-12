"""Ustawienia aplikacji: formularz otwarty i zapisany bez zmian daje dokładnie tę samą specyfikację (1:1)."""

import re
from html.parser import HTMLParser

import pytest

from deployra_common.schemas import AppSpec
from deployra_panel.services import deploy
from test_resources import job_of, nodes, wait_job  # noqa: F401 — fixture `nodes`


class EditForm(HTMLParser):
    """Pola formularza edycji tak, jak wyśle je przeglądarka z Alpine (`:disabled`, `x-model`, `:value`)."""

    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        top = re.search(r"kind: '(\w+)', runtime: '([^']*)', rport: '([^']*)',\s*mem: '([^']*)'", html)
        self.ctx = dict(zip(("kind", "runtime", "rport", "mem"), top.groups()))
        found = re.search(r"gstatic: (true|false)", html)
        self.ctx["gstatic"] = bool(found and found.group(1) == "true")
        self.inside, self.textarea, self.data = False, None, {}
        self.feed(html)

    def _js(self, expr: str) -> bool:
        py = expr.replace("!==", "!=").replace("===", "==").replace("||", " or ").replace("&&", " and ")
        return bool(eval(re.sub(r"!(?!=)", " not ", py), {}, dict(self.ctx)))  # noqa: S307 — tylko wyrażenia z naszego szablonu

    def _add(self, attrs: dict, value: str) -> None:
        if attrs.get("name") and "disabled" not in attrs and not (":disabled" in attrs and self._js(attrs[":disabled"])):
            self.data.setdefault(attrs["name"], []).append(value)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.inside = "/edit" in (attrs.get("hx-post") or "")
        if not self.inside:
            return
        if tag == "input":
            if attrs.get("type") == "checkbox":
                if "checked" in attrs:
                    self._add(attrs, attrs.get("value") or "on")
            elif "x-model" in attrs:
                self._add(attrs, str(self.ctx[attrs["x-model"]]))
            elif ":value" in attrs:
                self._add(attrs, str(self.ctx[attrs[":value"]]))
            else:
                self._add(attrs, attrs.get("value") or "")
        elif tag == "textarea":
            self.textarea = [attrs, ""]

    def handle_data(self, data):
        if self.textarea is not None:
            self.textarea[1] += data

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False
        elif tag == "textarea" and self.textarea is not None:
            attrs, text = self.textarea
            self.textarea = None
            self._add(attrs, text[1:] if text.startswith("\n") else text)  # przeglądarka pomija pierwszą nową linię


SPECS = {
    "przejety": ({"source": {"kind": "image", "image": "ghcr.io/ktos/app:1.2", "command": ["sh", "-c", "echo 'a b' && run"]},
                  "container_port": 80, "host_port": 8084, "memory_mb": 256, "cpus": 0.25, "domains": ["app.example.com"],
                  "env": {"A": "1", "MULTI": "linia1\nlinia2", "SP": " spacje ", "Q": '"w cudzysłowie"', "EQ": "a=b", "PUSTE": ""},
                  "volumes": [{"source": "dane", "target": "/data"}, {"source": "/srv/x", "target": "/x", "read_only": True}]},
                 {"https": True, "force_ssl": True, "dns": True}),
    "compose": ({"source": {"kind": "compose", "compose": "\nservices:\n  web:\n    image: nginx:alpine  \n", "expose_service": "web"},
                 "container_port": 80, "domains": ["c.example.com"]}, {"https": False}),
    "budowany": ({"source": {"kind": "git", "git_url": "https://github.com/ktos/repo.git", "git_branch": "dev", "git_token": "tajny",
                             "dockerfile": "docker/Dockerfile", "build_context": "app"}, "container_port": 3000}, {}),
    "strona-git": ({"source": {"kind": "git", "git_url": "git@github.com:ktos/www.git", "git_static_dir": "public"}}, {}),
    "flask": ({"source": {"kind": "runtime", "runtime": "python-flask", "start_command": "gunicorn -b 0.0.0.0:8000 app:app"},
               "memory_mb": 192, "domains": ["f.example.com"]}, {"https": True}),
    "bot": ({"source": {"kind": "runtime", "runtime": "python-script"}}, {}),
    "statyczna": ({"source": {"kind": "static"}, "domains": ["s.example.com"]}, {"https": True, "force_ssl": False}),
}


@pytest.mark.parametrize("slug", SPECS)
def test_settings_form_roundtrip(admin, nodes, slug):
    panel = admin.app.state.panel
    raw, options = SPECS[slug]
    spec = AppSpec.model_validate({"slug": slug, **raw})
    app_id = deploy.create_app_row(panel, slug, 1, spec, options)
    panel.db.update("apps", app_id, host_port=20005, status="running")
    page = admin.get(f"/apps/{app_id}/edit", headers={"HX-Request": ""})
    assert page.status_code == 200
    form = EditForm(page.text).data
    assert "container_port" not in form or len(form["container_port"]) == 1, form  # pola innych rodzajów wyłączone

    response = admin.post(f"/apps/{app_id}/edit", data={**form, "deploy": ""})
    assert response.status_code == 204 and "job=" not in response.headers["HX-Redirect"], response.headers
    saved, saved_options = deploy.load(panel, panel.db.get("apps", app_id))
    assert saved.model_dump() == spec.model_dump()
    assert {k: bool(saved_options.get(k)) for k in ("https", "force_ssl", "dns")} == {k: bool(options.get(k)) for k in ("https", "force_ssl", "dns")}

    # sama zmiana domeny w przejętym kontenerze → bez wdrożenia na węźle, port hosta zostaje
    if slug == "przejety":
        form["domains"] = ["nowa.example.com"]
        form.pop("https"), form.pop("force_ssl")  # bez bramki w testach nie ma certyfikatu
        job = wait_job(panel, job_of(admin.post(f"/apps/{app_id}/edit", data={**form, "deploy": "1"})))
        assert job.status == "ok" and "/api/apps/deploy" not in nodes[1].jobs(), (job.error, job.lines[-5:])
        saved, _ = deploy.load(panel, panel.db.get("apps", app_id))
        assert saved.host_port == 8084 and saved.domains == ["nowa.example.com"]

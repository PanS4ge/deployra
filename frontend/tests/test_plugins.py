import io
import json
import shutil
import time
import zipfile
from pathlib import Path

from deployra_panel.i18n import catalog

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "plugins"

DEMO_CODE = '''
from fastapi import Request

received = []


def setup(api):
    router = api.router()
    api.add_nav("title", "/plugins/demo", "stars")
    api.on("app.deployed", lambda event, data: received.append((event, data)))
    api.add_app_template({"id": "echo", "name": "Echo", "image": "ealen/echo-server", "container_port": 80, "env": {"KEY": "${RANDOM}"}})

    @router.get("")
    def page(request: Request):
        return api.render(request, "page.html", title=api.t("title"), value=api.setting("value", "none"))
'''


def write_plugin(root: Path, plugin_id: str, manifest: dict | None = None, code: str | None = None, files: dict | None = None) -> Path:
    path = root / plugin_id
    path.mkdir(parents=True)
    (path / "plugin.json").write_text(json.dumps({"id": plugin_id, "name": plugin_id.title(), **(manifest or {})}), encoding="utf-8")
    if code is not None:
        (path / "plugin.py").write_text(code, encoding="utf-8")
    for name, content in (files or {}).items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(content, encoding="utf-8")
    return path


def demo(root: Path) -> Path:
    return write_plugin(root, "demo", {"entry": "plugin.py"}, DEMO_CODE, {
        "templates/page.html": '{% extends "base.html" %}{% block content %}<p id="demo">{{ t("plugin.demo.hello", value=value) }}</p>{% endblock %}',
        "locales/pl.json": json.dumps({"plugin": {"demo": {"title": "Demo PL", "hello": "Cześć {value}"}}}),
        "locales/en.json": json.dumps({"plugin": {"demo": {"title": "Demo EN", "hello": "Hello {value}"}}}),
        "static/hello.txt": "static ok",
    })  # fmt: skip


def zip_bytes(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buffer.getvalue()


def add_node(panel) -> None:
    panel.db.insert("nodes", name="dom", transport="direct", api_url="http://10.0.0.2:2750", status="paired")


def test_builtin_catalog_fills_the_wizard(admin):
    add_node(admin.app.state.panel)
    page = admin.get("/settings/plugins").text
    assert "Katalog aplikacji" in page and "wbudowany" in page

    wizard = admin.get("/apps/new").text
    assert "Uptime Kuma" in wizard and "apps-catalog:vaultwarden" in wizard

    prefilled = admin.get("/apps/new?template=apps-catalog:vaultwarden").text
    assert "vaultwarden/server:latest" in prefilled and 'name="env_key" value="ADMIN_TOKEN"' in prefilled and "${RANDOM}" not in prefilled

    wordpress = admin.app.state.panel.plugins.app_template("apps-catalog:wordpress")
    assert wordpress["kind"] == "compose" and "${DB_PASSWORD}" in wordpress["source"]["compose"]
    password = wordpress["env"].strip().split("=", 1)[1]
    assert len(password) >= 24 and "RANDOM" not in password
    assert admin.app.state.panel.plugins.app_template("apps-catalog:nope") is None


def test_disabling_builtin_hides_templates(admin):
    add_node(admin.app.state.panel)
    assert admin.post("/settings/plugins/apps-catalog/disable").status_code == 204
    assert "Uptime Kuma" not in admin.get("/apps/new").text
    assert admin.get("/apps/new?template=apps-catalog:gitea").status_code == 200  # nieznany szablon = pusty kreator
    assert admin.post("/settings/plugins/apps-catalog/delete").status_code == 400  # wbudowanego nie usuwamy
    admin.post("/settings/plugins/apps-catalog/enable")
    assert "Uptime Kuma" in admin.get("/apps/new").text


def test_code_plugin_lifecycle(admin, config):
    panel = admin.app.state.panel
    add_node(panel)
    demo(config.plugins_dir)
    assert admin.post("/settings/plugins/rescan").status_code == 204
    plugin = panel.plugins.plugins["demo"]
    assert not plugin.enabled and plugin.capabilities == ["code", "locales", "pages"]
    assert admin.get("/plugins/demo").status_code == 404  # kod wgranego pluginu nie startuje sam

    assert admin.post("/settings/plugins/demo/enable").status_code == 204
    assert plugin.enabled and plugin.loaded and plugin.error is None
    page = admin.get("/plugins/demo", headers={"HX-Request": ""})
    assert page.status_code == 200 and "Cześć none" in page.text and 'href="/plugins/demo"' in page.text and "Demo PL" in page.text
    assert admin.get("/plugins/demo/static/hello.txt").text == "static ok"
    assert admin.get("/plugins/demo/static/..%2Fplugin.py").status_code == 404
    assert "demo:echo" in admin.get("/apps/new").text

    panel.plugins.emit("app.deployed", app={"name": "web"}, job="j1")
    received = plugin.module.received
    for _ in range(50):
        if received:
            break
        time.sleep(0.05)
    assert received == [("app.deployed", {"app": {"name": "web"}, "job": "j1"})]

    assert admin.post("/settings/plugins/demo/disable").status_code == 204
    assert admin.get("/plugins/demo").status_code == 404
    assert admin.get("/plugins/demo/static/hello.txt").status_code == 404
    assert 'href="/plugins/demo"' not in admin.get("/").text
    panel.plugins.emit("app.deployed", app={"name": "web"}, job="j2")
    time.sleep(0.2)
    assert len(received) == 1

    # stan przetrwa restart panelu
    assert panel.db.setting("plugins")["disabled"] == ["demo"]


def test_broken_plugins_do_not_break_the_panel(admin, config):
    panel = admin.app.state.panel
    write_plugin(config.plugins_dir, "boom", {"entry": "plugin.py"}, "def setup(api):\n    raise RuntimeError('kaboom')\n")
    write_plugin(config.plugins_dir, "nosetup", {"entry": "plugin.py"}, "x = 1\n")
    write_plugin(config.plugins_dir, "mismatch", {"id": "other"})
    write_plugin(config.plugins_dir, "badentry", {"entry": "../evil.py"})
    write_plugin(config.plugins_dir, "future", {"requires": "99.0.0"})
    (config.plugins_dir / "nojson").mkdir()
    panel.plugins.discover()

    assert admin.post("/settings/plugins/boom/enable").status_code == 204
    boom = panel.plugins.plugins["boom"]
    assert not boom.enabled and "kaboom" in boom.error and not boom.loaded
    admin.post("/settings/plugins/nosetup/enable")
    assert "setup(api)" in panel.plugins.plugins["nosetup"].error

    for plugin_id in ("mismatch", "badentry", "future", "nojson"):
        plugin = panel.plugins.plugins[plugin_id]
        assert plugin.manifest is None and plugin.error, plugin_id
        assert admin.post(f"/settings/plugins/{plugin_id}/enable").status_code == 400
    assert "99.0.0" in panel.plugins.plugins["future"].error

    page = admin.get("/settings/plugins")
    assert page.status_code == 200 and "kaboom" in page.text
    assert admin.get("/").status_code == 200


def test_zip_upload_installs_disabled_and_rejects_traversal(admin, config):
    panel = admin.app.state.panel
    archive = zip_bytes({
        "zipped/plugin.json": json.dumps({"id": "zipped", "name": "Zipped", "entry": "plugin.py"}),
        "zipped/plugin.py": "def setup(api):\n    api.add_nav('Zipped', '/plugins/zipped')\n",
    })  # fmt: skip
    response = admin.post("/settings/plugins/upload", files={"file": ("zipped.zip", archive, "application/zip")})
    assert response.status_code == 204
    assert (config.plugins_dir / "zipped" / "plugin.py").is_file()
    assert not panel.plugins.plugins["zipped"].enabled
    assert not list(config.plugins_dir.glob(".zipped-*"))

    evil = zip_bytes({"plugin.json": json.dumps({"id": "evil", "name": "Evil"}), "../../escape.txt": "x"})
    response = admin.post("/settings/plugins/upload", files={"file": ("evil.zip", evil, "application/zip")})
    assert response.status_code == 400 and "evil" not in panel.plugins.plugins
    assert not (config.plugins_dir.parent / "escape.txt").exists() and not (config.plugins_dir / "evil").exists()

    for bad in (b"not a zip", zip_bytes({"readme.txt": "?"}), zip_bytes({"plugin.json": "{"})):
        assert admin.post("/settings/plugins/upload", files={"file": ("bad.zip", bad, "application/zip")}).status_code == 400
    assert admin.post("/settings/plugins/upload", files={"file": ("bad.tar", b"x", "application/x-tar")}).status_code == 400
    builtin = zip_bytes({"plugin.json": json.dumps({"id": "apps-catalog", "name": "Fake"})})
    assert admin.post("/settings/plugins/upload", files={"file": ("b.zip", builtin, "application/zip")}).status_code == 400

    assert admin.post("/settings/plugins/zipped/delete").status_code == 204
    assert not (config.plugins_dir / "zipped").exists() and "zipped" not in panel.plugins.plugins


def test_plugin_can_add_a_language(admin, config):
    panel = admin.app.state.panel
    write_plugin(config.plugins_dir, "lang-de", files={"locales/de.json": json.dumps({"_language": "Deutsch", "nav": {"apps": "Anwendungen"}})})
    panel.plugins.discover()
    assert "de" not in catalog.languages
    admin.post("/settings/plugins/lang-de/enable")
    assert "de" in catalog.languages and catalog.names()["de"] == "Deutsch"

    assert admin.post("/language", data={"lang": "de"}).status_code == 204
    page = admin.get("/", headers={"HX-Request": ""}).text
    assert '<html lang="de">' in page and "Anwendungen" in page and "Settings" in page  # brakujące klucze spadają na angielski
    admin.post("/language", data={"lang": "pl"})

    admin.post("/settings/plugins/lang-de/disable")
    assert "de" not in catalog.languages


def test_example_events_plugin(admin, config):
    panel = admin.app.state.panel
    shutil.copytree(EXAMPLES / "events-log", config.plugins_dir / "events-log")
    panel.plugins.discover()
    assert admin.post("/settings/plugins/events-log/enable").status_code == 204
    assert panel.plugins.plugins["events-log"].error is None
    assert "Dziennik zdarzeń" in admin.get("/").text

    assert admin.post("/plugins/events-log/test").status_code == 200
    for _ in range(50):
        page = admin.get("/plugins/events-log", headers={"HX-Request": ""}).text
        if "notification" in page:
            break
        time.sleep(0.05)
    assert "notification" in page and "Test pluginu" in page

    assert admin.post("/plugins/events-log/webhook", data={"url": "ftp://x"}).status_code == 400
    assert admin.post("/plugins/events-log/webhook", data={"url": "https://ntfy.sh/deployra"}).status_code == 200
    assert panel.db.setting("plugin.events-log.webhook") == "https://ntfy.sh/deployra"

import asyncio
import io
import json
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from deployra_common.jobs import JobFailed
from deployra_panel.services import alerts, deploy, migrate, notify, relay, resources


def host(ram=40.0, cpu=10.0, disk=30.0, free_mb=2048):
    total = 4096 * 1024 * 1024
    return {"cpu": cpu, "ram": ram, "disk": disk, "swap": 0, "load": [0.1, 0.1, 0.1], "ram_total": total,
            "ram_used": total - free_mb * 1024 * 1024, "disk_total": 100, "disk_used": 30}  # fmt: skip


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    async def aiter_bytes(self, size=1 << 20):
        yield self.body


class FakeNode:
    def __init__(self, usage=None, port=20001):
        self.usage, self.port, self.calls, self.fail_on = usage, port, [], None

    async def get(self, path, **_):
        self.calls.append(("GET", path))
        if self.usage is None:
            raise httpx.ConnectError("down")
        return self.usage if path == "/api/usage" else self.usage["host"]

    async def post(self, path, json=None, **_):
        self.calls.append(("POST", path, json))
        return {}

    async def delete(self, path, **_):
        self.calls.append(("DELETE", path))
        return {}

    async def upload_file(self, path, file, **_):
        self.calls.append(("UPLOAD", path, Path(file).read_bytes()))
        return {"transfer": "b" * 16, "files": 2}

    @asynccontextmanager
    async def stream(self, method, path, **_):
        self.calls.append(("STREAM", path))
        yield FakeResponse(b"bundle")

    def jobs(self):
        return [c[1] for c in self.calls if c[0] == "JOB"]


async def fake_follow(client, path, body, job, prefix=""):
    client.calls.append(("JOB", path, body))
    if client.fail_on and client.fail_on in path:
        raise JobFailed(f"boom {path}")
    if path.endswith("/export"):
        return {"result": {"transfer": "a" * 16}}
    if path == "/api/apps/deploy":
        return {"result": {"host_port": client.port if body.get("container_port") else None}}
    return {"result": {}}


@pytest.fixture
def nodes(admin, monkeypatch):
    panel = admin.app.state.panel
    resources.LATEST.clear()
    fakes = {}
    for name, port in (("dom", 20001), ("vps", 20002)):
        node_id = panel.db.insert("nodes", name=name, transport="direct", api_url="http://127.0.0.1:1", status="paired")
        fakes[node_id] = FakeNode(usage={"host": host(), "apps": {}, "containers": []}, port=port)
    monkeypatch.setattr(panel, "node_client", lambda node: fakes[node["id"]])
    monkeypatch.setattr(relay, "start_and_follow", fake_follow)
    return fakes


def wait_job(panel, job_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = panel.jobs.get(job_id)
        if job.done:
            return job
        time.sleep(0.05)
    raise AssertionError("zadanie nie skończyło się")


def job_of(response):
    return response.headers["HX-Redirect"].split("job=")[1]


def capture(monkeypatch):
    sent = []

    async def fake_send(panel, channel_id, message):
        sent.append((channel_id, message))

    monkeypatch.setattr(notify, "send_to_channel", fake_send)
    return sent


# --- kanały powiadomień ---------------------------------------------------------------------


def test_channel_formats():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={})

    message = notify.Message("RAM <dom>", "95% & rośnie", "err", "node_ram", {"node": "dom"})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await notify.send("webhook", {"url": "https://h.example/x", "secret": "s3"}, message, client)
            await notify.send("discord", {"url": "https://discord.com/api/webhooks/1/a"}, message, client)
            await notify.send("telegram", {"token": "123:ABC", "chat_id": "42"}, message, client)
            await notify.send("ntfy", {"url": "https://ntfy.sh/alerty", "token": "tk"}, message, client)
            await notify.send("gotify", {"url": "https://gotify.example/", "token": "gk"}, message, client)
            await notify.send("pushover", {"user": "u", "token": "p"}, message, client)
            await notify.send("slack", {"url": "https://hooks.slack.com/services/x"}, message, client)

    asyncio.run(go())
    webhook, discord, telegram, ntfy, gotify, pushover, slack = requests
    import hashlib
    import hmac

    assert webhook.headers["X-Deployra-Signature"] == "sha256=" + hmac.new(b"s3", webhook.content, hashlib.sha256).hexdigest()
    assert json.loads(webhook.content)["event"] == "node_ram"
    assert json.loads(discord.content)["embeds"][0]["color"] == notify.LEVEL_COLORS["err"]
    assert str(telegram.url) == "https://api.telegram.org/bot123:ABC/sendMessage" and "&lt;dom&gt;" in json.loads(telegram.content)["text"]
    assert str(ntfy.url) == "https://ntfy.sh/" and json.loads(ntfy.content)["topic"] == "alerty" and ntfy.headers["Authorization"] == "Bearer tk"
    assert str(gotify.url) == "https://gotify.example/message" and gotify.headers["X-Gotify-Key"] == "gk"
    assert b"priority=1" in pushover.content and "*RAM <dom>*" in json.loads(slack.content)["text"]


def test_email_channel(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=0):
            sent.update(host=host, port=port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            sent["tls"] = True

        def login(self, user, password):
            sent["login"] = (user, password)

        def send_message(self, mail):
            sent["subject"], sent["to"] = mail["Subject"], mail["To"]

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    config = {"host": "smtp.example.com", "port": "", "security": "starttls", "username": "a", "password": "b",
              "sender": "d@example.com", "to": "x@example.com"}  # fmt: skip
    notify._send_mail(config, notify.Message("Test", "treść"))
    assert sent == {"host": "smtp.example.com", "port": 587, "tls": True, "login": ("a", "b"), "subject": "[Deployra] Test", "to": "x@example.com"}


def test_channels_and_rules_ui(admin, monkeypatch):
    panel = admin.app.state.panel
    assert admin.post("/settings/alerts/channels", data={"kind": "webhook", "f_url": "ftp://x"}).status_code == 400
    assert admin.post("/settings/alerts/channels", data={"kind": "telegram", "f_token": "123456:SEKRET"}).status_code == 400  # brak chat_id
    assert admin.post("/settings/alerts/channels", data={"kind": "telegram", "name": "TG", "f_token": "123456:SEKRET", "f_chat_id": "7"}).status_code == 204
    channel = panel.db.fetchone("SELECT * FROM notify_channels")
    assert "123456:SEKRET" not in channel["config"] and notify.channel_config(panel, channel)["token"] == "123456:SEKRET"  # zaszyfrowane
    assert channel["id"] in alerts.settings(panel)["node_ram"]["channels"]  # nowy kanał dostaje wszystko

    page = admin.get("/settings/alerts", headers={"HX-Request": ""}).text
    assert "TG" in page and "123456:SEKRET" not in page  # sekretów nie odsyłamy do przeglądarki
    # edycja z pustym tokenem zostawia stary
    admin.post(f"/settings/alerts/channels/{channel['id']}", data={"name": "Telegram", "f_token": "", "f_chat_id": "8"})
    assert notify.channel_config(panel, panel.db.get("notify_channels", channel["id"])) == {"token": "123456:SEKRET", "chat_id": "8"}

    sent = capture(monkeypatch)
    assert admin.post(f"/settings/alerts/channels/{channel['id']}/test").status_code == 200
    assert sent[0][0] == channel["id"]

    form = {"node_ram.enabled": "1", "node_ram.threshold": "80", "node_ram.minutes": "2", "node_ram.channels": [str(channel["id"])],
            "node_ram.auto_move": "1", "node_cpu.threshold": "90", "node_cpu.minutes": "5"}  # fmt: skip
    assert admin.post("/settings/alerts/rules", data=form).status_code == 200
    saved = alerts.settings(panel)
    assert saved["node_ram"] == {"enabled": True, "threshold": 80.0, "minutes": 2, "channels": [channel["id"]], "auto_move": True}
    assert saved["node_cpu"]["enabled"] is False and saved["node_cpu"]["channels"] == []
    assert admin.post("/settings/alerts/rules", data={"node_ram.threshold": "0"}).status_code == 400

    assert admin.post(f"/settings/alerts/channels/{channel['id']}/delete").status_code == 204
    assert alerts.settings(panel)["node_ram"]["channels"] == []


def test_legacy_webhook_becomes_channel(admin):
    panel = admin.app.state.panel
    panel.db.set_setting("notify_webhook", "https://discord.com/api/webhooks/1/abc")
    notify.migrate_legacy_webhook(panel)
    channel = panel.db.fetchone("SELECT * FROM notify_channels")
    assert channel["kind"] == "discord" and channel["id"] in alerts.settings(panel)["monitor_down"]["channels"]
    assert not panel.db.setting("notify_webhook")


# --- alerty ------------------------------------------------------------------------------------


def test_threshold_alerts_fire_and_resolve(admin, nodes, monkeypatch):
    panel = admin.app.state.panel
    node = panel.node(1)
    tracker, t0 = alerts.Tracker(), time.time()

    def run(usage, ts, apps=None):
        alerts.forget_settings_cache()
        return asyncio.run(alerts.evaluate_node(panel, tracker, node, usage, apps or {}, ts))

    assert run({"host": host(ram=95)}, t0) == []  # próg musi trwać 5 minut
    assert run({"host": host(ram=95)}, t0 + 301) == ["node_ram"]
    assert run({"host": host(ram=95)}, t0 + 400) == []  # już trwa — bez powtórek
    assert alerts.firing(panel)[0]["rule"] == "node_ram"
    assert any("RAM" in n["message"] for n in panel.db.fetchall("SELECT * FROM notifications"))
    run({"host": host(ram=88)}, t0 + 460)  # histereza: 88 > 90 - 5
    assert alerts.firing(panel)
    run({"host": host(ram=70)}, t0 + 520)
    assert not alerts.firing(panel)

    assert run(None, t0 + 600) == [] and run(None, t0 + 800) == ["node_offline"]
    run({"host": host()}, t0 + 860)
    assert not alerts.firing(panel)

    app = {"id": 7, "name": "Sklep", "slug": "sklep"}
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S.123456789Z", time.gmtime(t0 + 880))
    oom = {"sklep": {"mem": 250 * 1024**2, "limit": 256 * 1024**2, "restarts": 0, "oom_at": stamp}}
    assert run({"host": host(), "apps": oom}, t0 + 900, {"sklep": app}) == ["app_oom"]
    assert run({"host": host(), "apps": oom}, t0 + 960, {"sklep": app}) == []  # to samo OOM tylko raz
    oom["sklep"]["restarts"] = 4
    run({"host": host(), "apps": oom}, t0 + 1020, {"sklep": app})
    assert {a["rule"] for a in alerts.firing(panel)} == {"app_restarts"}

    # wyłączona reguła nie strzela, a kanał spoza listy nic nie dostaje
    sent = capture(monkeypatch)
    panel.db.set_setting("alerts", {"node_cpu": {"enabled": False}})
    assert run({"host": host(cpu=99)}, t0 + 2000) == [] and run({"host": host(cpu=99)}, t0 + 5000) == []
    assert sent == []


def test_monitor_down_routes_to_channels(admin, monkeypatch):
    panel = admin.app.state.panel
    sent = capture(monkeypatch)
    channel_id = notify.save_channel(panel, "Hook", "webhook", {"url": "https://h.example"})
    panel.db.set_setting("alerts", {"monitor_down": {"enabled": True, "channels": [channel_id]}})
    monitor = {"id": 3, "kind": "url", "name": "Strona"}
    asyncio.run(alerts.monitor_change(panel, monitor, "down", "HTTP 502"))
    asyncio.run(alerts.monitor_change(panel, monitor, "up", "HTTP 200"))
    assert [m.level for _, m in sent] == ["err", "ok"] and "HTTP 502" in sent[0][1].text
    assert not [n for n in panel.db.fetchall("SELECT * FROM notifications") if "Strona" in n["message"]]  # panelu nie było na liście
    assert panel.db.fetchone("SELECT * FROM alerts")["status"] == "resolved"


# --- zasoby, wykresy, ochrona przed przepełnieniem -------------------------------------------


def test_tick_stores_history_and_pages_render(admin, nodes):
    panel = admin.app.state.panel
    app_id = panel.db.insert("apps", slug="web", name="Web", node_id=1, kind="image", spec="{}", status="running")
    nodes[1].usage = {"host": host(ram=55), "containers": [{"name": "app-web", "app": "web", "state": "running", "cpu": 3.0,
                                                           "mem": 80 * 1024**2, "limit": 128 * 1024**2}],
                      "apps": {"web": {"cpu": 3.0, "mem": 80 * 1024**2, "limit": 128 * 1024**2, "restarts": 0}}}  # fmt: skip
    nodes[2].usage = None
    asyncio.run(resources.tick(panel, alerts.Tracker()))
    asyncio.run(resources.tick(panel, alerts.Tracker()))
    assert len(panel.db.fetchall("SELECT * FROM node_samples WHERE node_id = 1")) == 2
    assert panel.db.fetchone("SELECT * FROM node_hourly WHERE node_id = 1")["n"] == 2
    assert panel.db.fetchone("SELECT * FROM app_hourly WHERE app_id = ?", [app_id])["mem_max"] == 80 * 1024**2
    assert resources.node_free(1) == 2048 and resources.node_free(2) is None
    old = time.time() - 3 * 3600  # wykres potrzebuje co najmniej dwóch przedziałów
    panel.db.insert("node_samples", node_id=1, ts=old, cpu=5, ram=50, disk=30)
    panel.db.insert("node_hourly", node_id=1, hour=int(old // 3600) - 30, n=1, cpu_sum=5, ram_sum=50, cpu_max=5, ram_max=50, disk_max=30)
    panel.db.insert("app_samples", app_id=app_id, ts=old, cpu=1, mem=50 * 1024**2, mem_limit=128 * 1024**2)

    for span in ("24h", "7d", "30d"):
        page = admin.get(f"/nodes/1/resources?range={span}")
        assert page.status_code == 200 and "<svg" in page.text, span
    assert "app-web" in admin.get("/nodes/1/resources").text
    page = admin.get(f"/apps/{app_id}/resources")
    assert page.status_code == 200 and "<svg" in page.text
    assert "2048 MB" in admin.get("/apps/new", headers={"HX-Request": ""}).text


def test_first_deploy_checks_memory(admin, nodes):
    panel = admin.app.state.panel
    nodes[1].usage = {"host": host(free_mb=100), "apps": {}, "containers": []}
    spec = {"slug": "duza", "source": {"kind": "image", "image": "nginx"}, "container_port": 80, "memory_mb": 512}
    from deployra_common.schemas import AppSpec

    app_id = deploy.create_app_row(panel, "Duża", 1, AppSpec.model_validate(spec), {})
    job = wait_job(panel, job_of(admin.post(f"/apps/{app_id}/deploy")))
    assert job.status == "error" and "vps" in job.error and "512" in job.error
    assert "/api/apps/deploy" not in nodes[1].jobs()
    # ponowne wdrożenie to świadoma decyzja — już nie blokujemy
    job = wait_job(panel, job_of(admin.post(f"/apps/{app_id}/deploy")))
    assert job.status == "ok" and "/api/apps/deploy" in nodes[1].jobs()


# --- aplikacje z plików ----------------------------------------------------------------------


def zip_bytes():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("app.py", "print(1)\n")
    return buffer.getvalue()


def test_runtime_wizard_uploads_files_before_deploy(admin, nodes):
    panel = admin.app.state.panel
    response = admin.post("/apps/new", data={"kind": "runtime", "runtime": "python-flask", "name": "Hello", "slug": "hello", "node_id": "1",
                                             "domains": "hello.example.com", "memory_mb": "192"},
                          files={"file": ("site.zip", zip_bytes(), "application/zip")})  # fmt: skip
    assert response.status_code == 204, response.headers.get("HX-Trigger")
    wait_job(panel, job_of(response))
    calls = nodes[1].calls
    upload = next(i for i, c in enumerate(calls) if c[0] == "UPLOAD")
    deploy_call = next(i for i, c in enumerate(calls) if c[0] == "JOB" and c[1] == "/api/apps/deploy")
    assert upload < deploy_call and calls[upload][1] == "/api/apps/hello/upload"
    spec = calls[deploy_call][2]
    assert spec["source"]["runtime"] == "python-flask" and spec["container_port"] == 8000 and spec["memory_mb"] == 192
    app = panel.db.get("apps", "hello", key="slug")
    assert app["kind"] == "runtime" and app["status"] == "running"
    page = admin.get(f"/apps/{app['id']}", headers={"HX-Request": ""}).text
    assert "Flask" in page and "gunicorn" in page and f"/apps/{app['id']}/logs" in page

    # nowy ZIP → automatyczne ponowne wdrożenie; PHP widzi zmiany od razu
    again = admin.post(f"/apps/{app['id']}/upload", files={"file": ("site.zip", zip_bytes(), "application/zip")})
    assert again.status_code == 204 and "job=" in again.headers["HX-Redirect"]


def test_worker_has_no_domain(admin, nodes):
    panel = admin.app.state.panel
    response = admin.post("/apps/new", data={"kind": "runtime", "runtime": "python-script", "name": "Bot", "slug": "bot", "node_id": "1",
                                             "domains": "bot.example.com"})  # fmt: skip
    wait_job(panel, job_of(response))
    spec = next(c[2] for c in nodes[1].calls if c[0] == "JOB" and c[1] == "/api/apps/deploy")
    assert spec["container_port"] is None
    assert panel.db.get("apps", "bot", key="slug")["site_id"] is None
    php = admin.post("/apps/new", data={"kind": "runtime", "runtime": "php", "name": "Sklep", "slug": "sklep", "node_id": "1"})
    wait_job(panel, job_of(php))
    sklep = panel.db.get("apps", "sklep", key="slug")
    upload = admin.post(f"/apps/{sklep['id']}/upload", files={"file": ("s.zip", zip_bytes(), "application/zip")})
    assert upload.status_code == 200  # tylko toast


# --- przenoszenie ------------------------------------------------------------------------------


def make_app(admin, nodes, **extra):
    panel = admin.app.state.panel
    data = {"kind": "image", "image": "nginx", "container_port": "80", "name": "Web", "slug": "web", "node_id": "1",
            "domains": "web.example.com", **extra}  # fmt: skip
    wait_job(panel, job_of(admin.post("/apps/new", data=data)))
    return panel.db.get("apps", "web", key="slug")


def test_move_app_between_nodes(admin, nodes):
    panel = admin.app.state.panel
    app = make_app(admin, nodes)
    response = admin.post(f"/apps/{app['id']}/move", data={"node_id": "2", "delete_old": "1"})
    job = wait_job(panel, job_of(response))
    assert job.status == "ok", job.lines
    moved = panel.db.get("apps", app["id"])
    assert moved["node_id"] == 2 and moved["host_port"] == 20002 and moved["status"] == "running"
    site = panel.db.get("sites", moved["site_id"])
    assert site["target"]["node_id"] == 2 and site["target"]["port"] == 20002
    assert nodes[1].jobs()[-2:] == ["/api/apps/web/export", "/api/apps/web/remove"]
    assert ("DELETE", "/api/transfers/" + "a" * 16) in nodes[1].calls
    assert next(c for c in nodes[2].calls if c[0] == "UPLOAD")[2] == b"bundle"
    assert nodes[2].jobs() == ["/api/apps/web/import", "/api/apps/deploy"]
    remove = next(c for c in nodes[1].calls if c[0] == "JOB" and c[1].endswith("/remove"))
    assert remove[2] == {"delete_data": True}
    assert panel.db.fetchone("SELECT node_id FROM monitors WHERE kind = 'app' AND ref_id = ?", [app["id"]])["node_id"] == 2


def test_failed_move_returns_app(admin, nodes):
    panel = admin.app.state.panel
    app = make_app(admin, nodes)
    nodes[2].fail_on = "/api/apps/deploy"
    job = wait_job(panel, job_of(admin.post(f"/apps/{app['id']}/move", data={"node_id": "2"})))
    assert job.status == "error"
    back = panel.db.get("apps", app["id"])
    assert back["node_id"] == 1 and back["host_port"] == 20001 and back["status"] == "running"
    assert panel.db.get("sites", back["site_id"])["target"]["node_id"] == 1
    assert "/api/apps/web/power" in nodes[1].jobs() and "/api/apps/web/remove" in nodes[2].jobs()
    assert "/api/apps/web/remove" not in nodes[1].jobs()


def test_auto_move_only_for_stateless_opt_in(admin, nodes, monkeypatch):
    panel = admin.app.state.panel
    app = make_app(admin, nodes)
    started = []
    monkeypatch.setattr(migrate, "run", lambda panel, job, app_id, target_id, delete_old=False, auto=False: started.append((app_id, target_id)))
    node = panel.node(1)
    migrate._last_auto.clear()
    assert asyncio.run(migrate.relieve(panel, node)) is None  # aplikacja nie ma włączonego przenoszenia
    assert admin.post(f"/apps/{app['id']}/auto-move", data={"auto_move": "1"}).status_code == 200
    resources.LATEST[1] = {"ts": time.time(), "host": host(ram=97, free_mb=50), "apps": {"web": {"mem": 300 * 1024**2}}}
    job = asyncio.run(migrate.relieve(panel, node))
    wait_job(panel, job.id)
    assert started == [(app["id"], 2)]
    assert asyncio.run(migrate.relieve(panel, node)) is None  # godzina przerwy dla tego węzła

    with_volume = {"volumes": "dane:/data"}
    assert migrate.auto_capable(deploy.load(panel, app)[0])
    from deployra_common.schemas import AppSpec

    spec = AppSpec.model_validate({"slug": "db", "source": {"kind": "image", "image": "postgres"}, "container_port": 5432,
                                   "volumes": [{"source": "dane", "target": "/data"}]})  # fmt: skip
    assert not migrate.auto_capable(spec) and with_volume


def test_app_access_list(admin, nodes):
    panel = admin.app.state.panel
    app = make_app(admin, nodes)
    acl = panel.db.insert("access_lists", name="Rodzina", users=[{"username": "mama", "htpasswd": "$apr1$x$y"}], clients=[])
    assert "Rodzina" in admin.get(f"/apps/{app['id']}", headers={"HX-Request": ""}).text
    assert admin.post(f"/apps/{app['id']}/access", data={"access_list_id": str(acl)}).status_code == 200
    assert panel.db.get("sites", app["site_id"])["access_list_id"] == acl
    assert admin.post(f"/apps/{app['id']}/access", data={"access_list_id": ""}).status_code == 200
    assert panel.db.get("sites", app["site_id"])["access_list_id"] is None
    assert admin.post(f"/apps/{app['id']}/access", data={"access_list_id": "999"}).status_code == 400

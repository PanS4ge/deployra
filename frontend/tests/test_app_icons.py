import asyncio

import httpx

from deployra_common.schemas import AppSpec
from deployra_panel.services import app_icons, deploy

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_split_image_and_registry_candidates():
    assert app_icons.split_image("nginx:1.27") == ("docker.io", "library", "nginx")
    assert app_icons.split_image("louislam/uptime-kuma:1") == ("docker.io", "louislam", "uptime-kuma")
    assert app_icons.split_image("ghcr.io/PanS4ge/deployra-panel:1.3.0") == ("ghcr.io", "PanS4ge", "deployra-panel")
    assert app_icons.split_image("localhost:5000/app@sha256:abc") == ("localhost:5000", "", "app")
    hub = app_icons.registry_candidates("nginx")
    assert hub[0].endswith("/repos_logo/v1/library%2Fnginx") and hub[1].endswith("/png/nginx.png")
    assert app_icons.registry_candidates("ghcr.io/acme/tool:2")[-1] == "https://github.com/acme.png?size=128"


def test_favicon_links_prefer_large_icons():
    html = """<head><link rel="shortcut icon" href="/favicon.ico"><link rel="apple-touch-icon" href="touch.png">
              <link rel="icon" sizes="64x64" href="https://cdn.example/i64.png"><link rel="mask-icon" href="m.svg"></head>"""
    assert app_icons.favicon_links(html, "https://app.example/x/") == [
        "https://app.example/x/touch.png", "https://cdn.example/i64.png", "https://app.example/favicon.ico"]


def test_favicon_from_page_then_cache(panel, tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/":
            return httpx.Response(200, html='<link rel="icon" href="/static/logo.png">')
        if request.url.path == "/static/logo.png":
            return httpx.Response(200, content=PNG, headers={"content-type": "text/plain"})  # typ z nagłówka pliku
        return httpx.Response(404)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await app_icons._favicon(client, "app.example")

    data, kind = asyncio.run(run())
    assert data == PNG and kind == "image/png"
    assert seen == ["https://app.example/", "https://app.example/static/logo.png"]


def test_get_caches_hits_and_misses(panel, monkeypatch):
    node_id = panel.db.insert("nodes", name="n1", transport="direct", api_url="http://127.0.0.1:1", status="paired")
    spec = AppSpec.model_validate({"slug": "kuma", "node_id": node_id, "source": {"kind": "image", "image": "louislam/uptime-kuma:1"}, "container_port": 3001})
    app_id = deploy.create_app_row(panel, "Kuma", node_id, spec, {})
    calls = []

    async def fake_resolve(_panel, app, image):
        calls.append(image)
        return (PNG, "image/png") if len(calls) == 1 else None

    monkeypatch.setattr(app_icons, "_resolve", fake_resolve)
    app = panel.db.get("apps", app_id)
    assert asyncio.run(app_icons.get(panel, app)) == (PNG, "image/png")
    assert asyncio.run(app_icons.get(panel, app)) == (PNG, "image/png")  # z dysku, bez sieci
    assert calls == ["louislam/uptime-kuma:1"]
    app_icons.forget(panel, app_id)
    assert asyncio.run(app_icons.get(panel, app)) is None
    assert asyncio.run(app_icons.get(panel, app)) is None  # brak też jest zapamiętany
    assert len(calls) == 2


def test_icon_routes_upload_and_validation(admin):
    panel = admin.app.state.panel
    node_id = panel.db.insert("nodes", name="n1", transport="direct", api_url="http://127.0.0.1:1", status="paired")
    spec = AppSpec.model_validate({"slug": "web", "node_id": node_id, "source": {"kind": "image", "image": "nginx"}, "container_port": 80})
    app_id = deploy.create_app_row(panel, "Web", node_id, spec, {})

    assert admin.post(f"/apps/{app_id}/icon", data={"mode": "url", "url": "javascript:alert(1)"}).status_code == 400
    bad = admin.post(f"/apps/{app_id}/icon", data={"mode": "upload"}, files={"file": ("x.svg", b"<svg onload=alert(1)>", "image/svg+xml")})
    assert bad.status_code == 400
    ok = admin.post(f"/apps/{app_id}/icon", data={"mode": "upload"}, files={"file": ("logo.png", PNG, "image/png")})
    assert ok.status_code == 204
    assert panel.db.get("apps", app_id)["icon"].startswith("file:custom-")
    icon = admin.get(f"/apps/{app_id}/icon")
    assert icon.status_code == 200 and icon.content == PNG and "sandbox" in icon.headers["content-security-policy"]

    assert admin.post(f"/apps/{app_id}/icon", data={"mode": "auto"}).status_code == 204
    assert panel.db.get("apps", app_id)["icon"] is None
    assert not list(app_icons.icon_dir(panel).glob(f"custom-{app_id}-*"))


def test_move_card_shows_node_tiles(admin):
    import time

    from deployra_panel.services import resources

    panel = admin.app.state.panel
    here = panel.db.insert("nodes", name="oracle2", transport="direct", api_url="http://127.0.0.1:1", status="paired")
    big = panel.db.insert("nodes", name="cezary", transport="direct", api_url="http://127.0.0.1:2", status="paired")
    small = panel.db.insert("nodes", name="frog01", transport="direct", api_url="http://127.0.0.1:3", status="paired")
    spec = AppSpec.model_validate({"slug": "bot", "node_id": here, "source": {"kind": "image", "image": "bot:1"}, "container_port": 80})
    app_id = deploy.create_app_row(panel, "Bot", here, spec, {})
    gb = 1024 ** 3
    now = time.time()
    resources.LATEST.update({
        here: {"ts": now, "host": {"ram_total": 24 * gb, "ram_used": 20 * gb, "cpu": 10}, "apps": {"bot": {"mem": 300 * 1024 * 1024}}},
        big: {"ts": now, "host": {"ram_total": 64 * gb, "ram_used": 10 * gb, "cpu": 23, "disk_total": 100 * gb, "disk_used": 40 * gb}},
        small: {"ts": now, "host": {"ram_total": gb // 4, "ram_used": gb // 5, "cpu": 50}},
    })
    try:
        page = admin.get(f"/apps/{app_id}", headers={"HX-Request": ""}).text
    finally:
        for key in (here, big, small):
            resources.LATEST.pop(key, None)
    assert "node-pick" in page and "~300 MB" in page
    tiles = page.split('class="node-tile')[1:]
    assert "oracle2" in tiles[0] and "here" in tiles[0].split(">")[0]  # bieżący węzeł pierwszy, nieaktywny
    cezary = next(t for t in tiles if "cezary" in t)
    frog = next(t for t in tiles if "frog01" in t)
    assert "badge ok" in cezary and "60.0 GB" in cezary  # wolny dysk
    assert "off" in frog.split(">")[0] and "badge err" in frog  # 51 MB wolnego < 300 MB

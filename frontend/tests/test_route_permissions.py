"""Strażnik uprawnień: każda trasa panelu ma `require(...)`/`require_any(...)` albo jest na jednej z jawnych list.

Nowa trasa bez sprawdzenia uprawnień nie przejdzie testu po cichu — trzeba ją świadomie dopisać tutaj
(z powodem) albo dodać jej zależność z uprawnieniem."""

from __future__ import annotations

from fastapi.routing import APIRoute, APIWebSocketRoute

# bez sesji: logowanie, pierwsza konfiguracja, webhooki (HMAC), MCP (token), plugin Minecrafta (podpis)
PUBLIC = {
    ("GET", "/favicon.ico"), ("GET", "/healthz"), ("GET", "/login"), ("POST", "/login"), ("POST", "/logout"), ("POST", "/language"),
    ("GET", "/setup"), ("POST", "/setup"), ("GET", "/login/2fa"), ("POST", "/login/2fa"), ("GET", "/auth/forward"), ("HEAD", "/auth/forward"), ("GET", "/auth/forward/start"), ("GET", "/auth/forward/callback"),
    # logowanie kontami list dostępu na hostach aplikacji: token bramki (X-Deployra-Gw) + CSRF + limity w trasie
    ("GET", "/auth/forward/login"), ("POST", "/auth/forward/login"), ("POST", "/auth/forward/totp"), ("GET", "/auth/forward/logout"),
    ("POST", "/hooks/git/{app_id}"), ("POST", "/mcp"), ("GET", "/mcp/sse"), ("POST", "/mcp/messages"),
    ("GET", "/minecraft/download/DeployraBridge.jar"), ("POST", "/minecraft/bridge/pair"), ("POST", "/minecraft/bridge/poll"),
}

# własne sprawdzenie w środku: WebSockety (sesja + uprawnienie + Origin) i „Moje serwery” (przypisanie + jego uprawnienia)
SELF_CHECKED_PREFIXES = ("/servers/{grant_id}", "/nodes/{node_id}/containers/{cid}/terminal/ws", "/nodes/{node_id}/ssh/ws")

# podgląd dla każdego zalogowanego pełnego konta (konta „tylko przypisane” zatrzymuje middleware); bez sekretów
VIEW_ANY_ACCOUNT = {
    ("GET", "/"), ("GET", "/dashboard/nodes"), ("GET", "/dashboard/tunnel"), ("GET", "/notifications"),
    ("GET", "/analytics/spark"), ("GET", "/analytics/sparks"), ("GET", "/analytics/chart"),
    ("GET", "/security"), ("POST", "/security/snooze"),
    ("GET", "/jobs"), ("GET", "/jobs/table"), ("GET", "/jobs/{job_id}"), ("GET", "/jobs/{job_id}/stream"),
    ("GET", "/tunnel"), ("GET", "/tunnel/status"), ("GET", "/tunnel/external"), ("GET", "/uptime"),
    ("GET", "/nodes"), ("POST", "/nodes/refresh"), ("GET", "/nodes/{node_id}"), ("GET", "/nodes/{node_id}/vitals"),
    ("GET", "/nodes/{node_id}/resources"), ("POST", "/nodes/{node_id}/info"), ("POST", "/nodes/{node_id}/rps"),
    ("GET", "/nodes/{node_id}/ports"), ("GET", "/nodes/{node_id}/proxmox"), ("GET", "/nodes/{node_id}/containers"),
    ("GET", "/nodes/{node_id}/images"), ("GET", "/nodes/{node_id}/volumes"), ("GET", "/nodes/{node_id}/networks"),
    ("GET", "/nodes/{node_id}/stacks"),
    ("GET", "/apps"), ("GET", "/apps/docker-search"), ("GET", "/apps/containers"), ("GET", "/apps/{app_id}"),
    ("GET", "/apps/{app_id}/icon"), ("GET", "/apps/{app_id}/badge"), ("GET", "/apps/{app_id}/resources"), ("POST", "/apps/env-editor"),
    ("GET", "/proxy"), ("GET", "/proxy/streams"), ("GET", "/proxy/access"), ("GET", "/proxy/health"), ("GET", "/proxy/{kind_slug}"),
    ("GET", "/certs"), ("GET", "/dns"), ("GET", "/dns/records"), ("GET", "/servers"), ("GET", "/map"), ("GET", "/map/data"),
    ("GET", "/plugins/{plugin_id}/static/{path:path}"),
    # własne konto: bez uprawnień do kont widać i zmienia się tylko siebie (sprawdzane w trasie)
    ("GET", "/settings/users"), ("POST", "/settings/users/save"),
    ("GET", "/account"), ("POST", "/account/password"), ("POST", "/account/2fa/start"), ("POST", "/account/2fa/enable"),
    ("POST", "/account/2fa/backup"), ("POST", "/account/2fa/disable"), ("POST", "/account/sessions/{value}/revoke"),
    ("POST", "/account/sessions/revoke-others"),
}


def _walk(routes):
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _walk(inner.routes)
        else:
            yield route


def _calls(dependant, out):
    for sub in dependant.dependencies:
        out.append(getattr(sub.call, "__qualname__", repr(sub.call)))
        _calls(sub, out)
    return out


def test_every_route_checks_permissions(client):
    unguarded = []
    for route in _walk(client.app.router.routes):
        if not isinstance(route, (APIRoute, APIWebSocketRoute)):
            continue
        if any(name.startswith(("require.", "require_any.")) for name in _calls(route.dependant, [])):
            continue
        if route.path.startswith(SELF_CHECKED_PREFIXES):
            continue
        for method in sorted(getattr(route, "methods", None) or {"WS"}):
            if (method, route.path) not in PUBLIC | VIEW_ANY_ACCOUNT:
                unguarded.append(f"{method} {route.path}")
    assert unguarded == [], "trasy bez sprawdzenia uprawnień (dodaj require(...) albo dopisz świadomie do listy):\n" + "\n".join(unguarded)


def test_secrets_need_permissions(client, admin):
    """Konto bez uprawnień nie widzi compose/.env stacka, kont SFTP ani ustawień węzła; widzi tylko swoje konto."""
    panel = admin.app.state.panel
    node_id = panel.db.insert("nodes", name="n1", transport="direct", api_url="http://127.0.0.1:1", status="paired", secret="x")
    from deployra_common.passwords import hash_password

    panel.db.insert("users", email="viewer@example.com", password_hash=hash_password("viewer-pass-1"), permissions="", created_at="x",
                    undeletable=0)  # fmt: skip
    client.post("/login", data={"email": "viewer@example.com", "password": "viewer-pass-1"})
    for path in (f"/nodes/{node_id}/stacks/web", f"/nodes/{node_id}/stacks/new", f"/nodes/{node_id}/sftp", f"/nodes/{node_id}/settings"):
        assert client.get(path).status_code == 403, path
    page = client.get("/settings/users").text
    assert "viewer@example.com" in page and "admin@" not in page

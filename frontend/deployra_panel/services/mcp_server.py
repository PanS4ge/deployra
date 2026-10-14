"""Serwer Model Context Protocol (MCP 2024-11-05) dla Deployry.

Umożliwia agentom AI (Claude Desktop, Cursor, Antigravity, Cline, Windsurf itp.)
bezpieczne zarządzanie węzłami, aplikacjami, kontenerami, proxy i kopiami zapasowymi.
Zero bloatu, natywna obsługa JSON-RPC 2.0 bez ciężkich zależności.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from .. import __version__
from ..permissions import Permission, has

if TYPE_CHECKING:
    from ..context import Panel

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "deployra-mcp", "version": __version__}


class ToolDef:
    def __init__(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: Callable[[Panel, dict[str, Any], dict[str, Any]], Coroutine[Any, Any, Any]],
        permission: Permission | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.handler = handler
        self.permission = permission

    def to_mcp(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


class McpRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    def register(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        permission: Permission | None = None,
    ):
        def decorator(fn: Callable[[Panel, dict[str, Any], dict[str, Any]], Coroutine[Any, Any, Any]]):
            self._tools[name] = ToolDef(name, description, input_schema, fn, permission)
            return fn

        return decorator

    def list_tools(self, user: dict[str, Any]) -> list[dict[str, Any]]:
        result = []
        for tool in self._tools.values():
            if tool.permission and not has(user, tool.permission):
                continue
            result.append(tool.to_mcp())
        return result

    async def call_tool(self, panel: Panel, user: dict[str, Any], name: str, args: dict[str, Any]) -> dict[str, Any]:
        tool = self._tools.get(name)
        if not tool:
            return {
                "content": [{"type": "text", "text": f"Tool '{name}' not found."}],
                "isError": True,
            }
        if tool.permission and not has(user, tool.permission):
            return {
                "content": [{"type": "text", "text": f"Permission denied: missing {tool.permission.name}."}],
                "isError": True,
            }
        try:
            raw_result = await tool.handler(panel, user, args)
            if isinstance(raw_result, (dict, list)):
                text = json.dumps(raw_result, indent=2, ensure_ascii=False)
            else:
                text = str(raw_result)
            return {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            }
        except Exception as exc:
            return {
                "content": [{"type": "text", "text": f"Error executing '{name}': {exc}"}],
                "isError": True,
            }


registry = McpRegistry()


def _as_json_obj(val: Any, default: Any = None) -> Any:
    if val is None:
        return default
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return default
    return default


# --- Definicje narzędzi (Tools) ---


@registry.register(
    name="deployra_system_status",
    description="Get overall Deployra health, connected nodes, deployed apps, active jobs, and system alerts.",
    input_schema={"type": "object", "properties": {}, "required": []},
)
async def _system_status(panel: Panel, user: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    nodes = panel.db.fetchall("SELECT id, name, status, transport, tunnel_ip FROM nodes")
    apps = panel.db.fetchall("SELECT id, name, slug, status FROM apps")
    alerts = panel.db.fetchall("SELECT id, rule, target_kind, target_name, message FROM alerts WHERE ignored_at IS NULL")
    active_jobs = [j.snapshot() for j in panel.jobs.list() if not j.done]
    return {
        "deployra_version": __version__,
        "nodes": {
            "total": len(nodes),
            "paired": sum(1 for n in nodes if n["status"] == "paired"),
            "items": nodes,
        },
        "apps": {
            "total": len(apps),
            "running": sum(1 for a in apps if a["status"] == "running"),
            "stopped": sum(1 for a in apps if a["status"] == "stopped"),
            "error": sum(1 for a in apps if a["status"] == "error"),
        },
        "active_jobs_count": len(active_jobs),
        "active_alerts_count": len(alerts),
        "alerts": alerts[:5],
    }


@registry.register(
    name="deployra_list_nodes",
    description="List all servers/nodes connected to Deployra with their IP, transport, pairing status, and vitals summary.",
    input_schema={"type": "object", "properties": {}, "required": []},
)
async def _list_nodes(panel: Panel, user: dict[str, Any], args: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = panel.db.fetchall("SELECT id, name, transport, tunnel_ip, status, created_at, paired_at, info FROM nodes ORDER BY id")
    items = []
    for n in nodes:
        info = _as_json_obj(n.get("info"), {})
        items.append({
            "id": n["id"],
            "name": n["name"],
            "status": n["status"],
            "transport": n["transport"],
            "tunnel_ip": n["tunnel_ip"],
            "paired_at": n["paired_at"],
            "vitals": info.get("vitals", {}),
            "docker_version": info.get("docker", {}).get("version"),
        })
    return items


@registry.register(
    name="deployra_get_node",
    description="Get detailed information about a specific node: CPU, RAM, disk usage, containers count, open ports, and Proxmox details.",
    input_schema={
        "type": "object",
        "properties": {
            "node_id": {"type": "integer", "description": "The ID of the node"},
        },
        "required": ["node_id"],
    },
)
async def _get_node(panel: Panel, user: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    node_id = int(args["node_id"])
    node = panel.node(node_id)
    if not node:
        raise ValueError(f"Node {node_id} not found")

    client = panel.node_client(node)
    details: dict[str, Any] = {
        "id": node["id"],
        "name": node["name"],
        "status": node["status"],
        "transport": node["transport"],
        "tunnel_ip": node["tunnel_ip"],
        "lan_subnets": node.get("lan_subnets"),
    }
    if node["status"] == "paired":
        try:
            vitals = await client.get("/api/vitals")
            details["vitals"] = vitals
        except Exception as e:
            details["vitals_error"] = str(e)
        try:
            ports = await client.get("/api/system/ports")
            details["ports"] = ports
        except Exception:
            pass
        try:
            pve = await client.get("/api/system/proxmox")
            details["proxmox"] = pve
        except Exception:
            pass
    return details


@registry.register(
    name="deployra_list_apps",
    description="List all applications deployed via Deployra, including slug, status, domains, node, and ports.",
    input_schema={"type": "object", "properties": {}, "required": []},
)
async def _list_apps(panel: Panel, user: dict[str, Any], args: dict[str, Any]) -> list[dict[str, Any]]:
    rows = panel.db.fetchall(
        "SELECT a.id, a.name, a.slug, a.status, a.domains, a.node_id, a.host_port, a.last_deploy_at, "
        "n.name AS node_name FROM apps a LEFT JOIN nodes n ON n.id = a.node_id ORDER BY a.name"
    )
    result = []
    for r in rows:
        domains = _as_json_obj(r.get("domains"), [])
        result.append({
            "id": r["id"],
            "name": r["name"],
            "slug": r["slug"],
            "status": r["status"],
            "node_id": r["node_id"],
            "node_name": r["node_name"],
            "domains": domains,
            "host_port": r["host_port"],
            "last_deploy_at": r["last_deploy_at"],
        })
    return result


@registry.register(
    name="deployra_get_app",
    description="Get complete configuration and status of an application by ID or slug.",
    input_schema={
        "type": "object",
        "properties": {
            "app_id": {"type": "integer", "description": "The ID of the application"},
            "slug": {"type": "string", "description": "The slug of the application (if app_id not provided)"},
        },
    },
)
async def _get_app(panel: Panel, user: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    from . import deploy

    app = None
    if args.get("app_id"):
        app = panel.db.get("apps", int(args["app_id"]))
    elif args.get("slug"):
        app = panel.db.get("apps", str(args["slug"]), key="slug")
    if not app:
        raise ValueError("App not found")

    spec, options = deploy.load(panel, app)
    node = panel.node(app["node_id"])
    return {
        "id": app["id"],
        "name": app["name"],
        "slug": app["slug"],
        "status": app["status"],
        "node_id": app["node_id"],
        "node_name": node["name"] if node else None,
        "domains": spec.domains,
        "source_kind": spec.source.kind,
        "image": spec.source.image,
        "runtime": spec.source.runtime,
        "container_port": spec.container_port,
        "host_port": app.get("host_port"),
        "memory_mb": spec.memory_mb,
        "cpus": spec.cpus,
        "volumes": [{"source": v.source, "target": v.target} for v in spec.volumes],
        "options": options,
        "last_deploy_at": app.get("last_deploy_at"),
        "last_error": app.get("last_error"),
    }


@registry.register(
    name="deployra_deploy_app",
    description="Trigger a deployment or redeployment for an application.",
    input_schema={
        "type": "object",
        "properties": {
            "app_id": {"type": "integer", "description": "The application ID to redeploy"},
        },
        "required": ["app_id"],
    },
    permission=Permission.ALLOW_DEPLOYMENT,
)
async def _deploy_app(panel: Panel, user: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    from ..routes.apps import start_deploy

    app = panel.db.get("apps", int(args["app_id"]))
    if not app:
        raise ValueError(f"App {args['app_id']} not found")
    job = start_deploy(panel, app)
    panel.db.update("apps", app["id"], last_job=job.id)
    return {"status": "started", "job_id": job.id, "title": job.title}


@registry.register(
    name="deployra_app_action",
    description="Perform container lifecycle actions on an app: start, stop, restart, pause, unpause, kill.",
    input_schema={
        "type": "object",
        "properties": {
            "app_id": {"type": "integer", "description": "The application ID"},
            "action": {
                "type": "string",
                "enum": ["start", "stop", "restart", "pause", "unpause", "kill"],
                "description": "Lifecycle action to perform",
            },
        },
        "required": ["app_id", "action"],
    },
    permission=Permission.ALLOW_MODIFY_DOCKERS,
)
async def _app_action(panel: Panel, user: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    from . import deploy
    from ..routes.docker import _p
    from .uptime import app_containers

    app = panel.db.get("apps", int(args["app_id"]))
    if not app:
        raise ValueError("App not found")
    action = args["action"]
    node = panel.node(app["node_id"])
    if not node or node["status"] != "paired":
        raise ValueError("Node is offline or not paired")

    spec, _ = deploy.load(panel, app)
    ep = spec.endpoint or "local"
    client = panel.node_client(node)

    items = await client.get(_p("/api/containers", ep))
    mine = app_containers(items, app)
    target_ids = [c["id"] for c in mine] if mine else [f"app-{app['slug']}"]
    for cid in target_ids:
        await client.post(_p(f"/api/containers/{cid}/action/{action}", ep))

    new_state = "running" if action in ("start", "unpause", "restart") else ("paused" if action == "pause" else "stopped")
    panel.db.update("apps", app["id"], status=new_state)
    return {"app": app["slug"], "action": action, "status": new_state}


@registry.register(
    name="deployra_get_app_logs",
    description="Fetch recent console logs from an application container.",
    input_schema={
        "type": "object",
        "properties": {
            "app_id": {"type": "integer", "description": "The application ID"},
            "tail": {"type": "integer", "description": "Number of log lines to retrieve (default: 100)", "default": 100},
        },
        "required": ["app_id"],
    },
)
async def _get_app_logs(panel: Panel, user: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    from ..routes.docker import _p

    app = panel.db.get("apps", int(args["app_id"]))
    if not app:
        raise ValueError("App not found")
    node = panel.node(app["node_id"])
    if not node or node["status"] != "paired":
        raise ValueError("Node is offline")

    tail = min(int(args.get("tail") or 100), 500)
    client = panel.node_client(node)
    cid = f"app-{app['slug']}"
    logs_res = await client.get(_p(f"/api/containers/{cid}/logs", "local"), params={"tail": tail})
    return {"app": app["slug"], "tail": tail, "lines": logs_res.get("lines", [])}


@registry.register(
    name="deployra_list_containers",
    description="List all Docker containers running on a specific node with their state, image, ports, and detected profile.",
    input_schema={
        "type": "object",
        "properties": {
            "node_id": {"type": "integer", "description": "The node ID to query"},
            "endpoint": {"type": "string", "description": "Docker endpoint (default: 'local')", "default": "local"},
        },
        "required": ["node_id"],
    },
)
async def _list_containers(panel: Panel, user: dict[str, Any], args: dict[str, Any]) -> list[dict[str, Any]]:
    from deployra_common.container_profiles import detect_profile
    from ..routes.docker import _p

    node_id = int(args["node_id"])
    node = panel.node(node_id)
    if not node or node["status"] != "paired":
        raise ValueError("Node is not available")

    ep = args.get("endpoint") or "local"
    client = panel.node_client(node)
    containers = await client.get(_p("/api/containers", ep))
    for c in containers:
        prof = detect_profile(c.get("name", ""), c.get("image", ""))
        c["profile"] = {
            "kind": prof.kind,
            "title": prof.title,
            "can_dump": prof.can_dump,
        }
    return containers


@registry.register(
    name="deployra_container_action",
    description="Execute container lifecycle actions (start, stop, restart, pause, unpause, kill) on a node.",
    input_schema={
        "type": "object",
        "properties": {
            "node_id": {"type": "integer", "description": "Node ID"},
            "container_id": {"type": "string", "description": "Container ID or name"},
            "action": {
                "type": "string",
                "enum": ["start", "stop", "restart", "pause", "unpause", "kill"],
                "description": "Action to execute",
            },
            "endpoint": {"type": "string", "description": "Docker endpoint (default 'local')", "default": "local"},
        },
        "required": ["node_id", "container_id", "action"],
    },
    permission=Permission.ALLOW_MODIFY_DOCKERS,
)
async def _container_action(panel: Panel, user: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    from ..routes.docker import _p

    node = panel.node(int(args["node_id"]))
    if not node or node["status"] != "paired":
        raise ValueError("Node not found or offline")
    client = panel.node_client(node)
    ep = args.get("endpoint") or "local"
    cid = args["container_id"]
    action = args["action"]
    await client.post(_p(f"/api/containers/{cid}/action/{action}", ep))
    return {"node_id": node["id"], "container_id": cid, "action": action, "status": "ok"}


@registry.register(
    name="deployra_container_quick_action",
    description="Run database or server maintenance actions directly inside container (e.g. vacuum, db_size, optimize, bgsave, test_config, reload).",
    input_schema={
        "type": "object",
        "properties": {
            "node_id": {"type": "integer", "description": "Node ID"},
            "container_id": {"type": "string", "description": "Container ID or name"},
            "action_id": {"type": "string", "description": "Action ID (vacuum, db_size, optimize, bgsave, test_config, reload, etc.)"},
        },
        "required": ["node_id", "container_id", "action_id"],
    },
    permission=Permission.ALLOW_MODIFY_DOCKERS,
)
async def _container_quick_action(panel: Panel, user: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    node = panel.node(int(args["node_id"]))
    if not node or node["status"] != "paired":
        raise ValueError("Node not found or offline")
    client = panel.node_client(node)
    cid = args["container_id"]
    aid = args["action_id"]
    res = await client.post(f"/api/containers/{cid}/action/quick/{aid}")
    return res


@registry.register(
    name="deployra_list_container_files",
    description="List files and directories inside a container path.",
    input_schema={
        "type": "object",
        "properties": {
            "node_id": {"type": "integer", "description": "Node ID"},
            "container_id": {"type": "string", "description": "Container ID or name"},
            "path": {"type": "string", "description": "Directory path (default: '/')", "default": "/"},
            "endpoint": {"type": "string", "description": "Docker endpoint ('local' or 'ct-100')", "default": "local"},
        },
        "required": ["node_id", "container_id"],
    },
    permission=Permission.ALLOW_CONTAINER_FILES,
)
async def _list_container_files(panel: Panel, user: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    from ..routes.docker import _p

    node = panel.node(int(args["node_id"]))
    if not node or node["status"] != "paired":
        raise ValueError("Node offline")
    client = panel.node_client(node)
    cid = args["container_id"]
    path = args.get("path") or "/"
    ep = args.get("endpoint") or "local"
    return await client.get(_p(f"/api/containers/{cid}/files", ep), params={"path": path})


@registry.register(
    name="deployra_read_container_file",
    description="Read file contents from a container (text files).",
    input_schema={
        "type": "object",
        "properties": {
            "node_id": {"type": "integer", "description": "Node ID"},
            "container_id": {"type": "string", "description": "Container ID or name"},
            "path": {"type": "string", "description": "File path inside container"},
            "endpoint": {"type": "string", "description": "Docker endpoint ('local' or 'ct-100')", "default": "local"},
        },
        "required": ["node_id", "container_id", "path"],
    },
    permission=Permission.ALLOW_CONTAINER_FILES,
)
async def _read_container_file(panel: Panel, user: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    from ..routes.docker import _p

    node = panel.node(int(args["node_id"]))
    if not node or node["status"] != "paired":
        raise ValueError("Node offline")
    client = panel.node_client(node)
    cid = args["container_id"]
    path = args["path"]
    ep = args.get("endpoint") or "local"
    return await client.get(_p(f"/api/containers/{cid}/files/read", ep), params={"path": path})


@registry.register(
    name="deployra_write_container_file",
    description="Write or edit a file inside a container.",
    input_schema={
        "type": "object",
        "properties": {
            "node_id": {"type": "integer", "description": "Node ID"},
            "container_id": {"type": "string", "description": "Container ID or name"},
            "path": {"type": "string", "description": "File path inside container"},
            "content": {"type": "string", "description": "File text content"},
            "endpoint": {"type": "string", "description": "Docker endpoint ('local' or 'ct-100')", "default": "local"},
        },
        "required": ["node_id", "container_id", "path", "content"],
    },
    permission=Permission.ALLOW_CONTAINER_FILES_WRITE,
)
async def _write_container_file(panel: Panel, user: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    from ..routes.docker import _p

    node = panel.node(int(args["node_id"]))
    if not node or node["status"] != "paired":
        raise ValueError("Node offline")
    client = panel.node_client(node)
    cid = args["container_id"]
    path = args["path"]
    content = args["content"]
    ep = args.get("endpoint") or "local"
    return await client.post(_p(f"/api/containers/{cid}/files/write", ep), json={"path": path, "content": content})



@registry.register(
    name="deployra_dump_database",
    description="Trigger a native compressed SQL database dump (.sql.gz) for PostgreSQL, MySQL, MariaDB, or MongoDB. Can optionally upload directly to S3/SFTP backup storage.",
    input_schema={
        "type": "object",
        "properties": {
            "node_id": {"type": "integer", "description": "Node ID"},
            "container_id": {"type": "string", "description": "Container ID or name"},
            "save_to_backup_storage": {"type": "boolean", "description": "Whether to save the dump directly to configured backup storage", "default": False},
        },
        "required": ["node_id", "container_id"],
    },
    permission=Permission.ALLOW_MODIFY_DOCKERS,
)
async def _dump_database(panel: Panel, user: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    node = panel.node(int(args["node_id"]))
    if not node or node["status"] != "paired":
        raise ValueError("Node offline")

    client = panel.node_client(node)
    cid = args["container_id"]
    save_storage = bool(args.get("save_to_backup_storage"))

    if save_storage:
        from .backup_storage import get_backup_storage

        storage = get_backup_storage(panel)
        if not storage.is_configured():
            raise ValueError("No backup storage configured in Settings -> Backup")

        content = await client.get_bytes(f"/api/containers/{cid}/action/quick/dump_sql")
        filename = f"dump-{cid}-{time.strftime('%Y%m%d-%H%M%S')}.sql.gz"
        remote_path = storage.upload(filename, content)
        return {
            "status": "saved_to_storage",
            "storage_type": storage.kind,
            "filename": filename,
            "remote_path": remote_path,
            "size_bytes": len(content),
        }
    else:
        # Check profile to verify can_dump
        prof = await client.get(f"/api/containers/{cid}/profile")
        return {
            "container": cid,
            "profile": prof,
            "dump_url": f"/nodes/{node['id']}/containers/{cid}/dump",
            "message": "Use GET /nodes/{node_id}/containers/{cid}/dump in browser to download .sql.gz or set save_to_backup_storage=True.",
        }


@registry.register(
    name="deployra_list_proxy_hosts",
    description="List all reverse proxy sites/domains configured in Deployra Gateway with their backend targets, SSL, and status.",
    input_schema={"type": "object", "properties": {}, "required": []},
)
async def _list_proxy_hosts(panel: Panel, user: dict[str, Any], args: dict[str, Any]) -> list[dict[str, Any]]:
    sites = panel.db.fetchall(
        "SELECT id, kind, domains, target, redirect, cert_id, ssl_forced, enabled, archived, app_id, source FROM sites ORDER BY id"
    )
    result = []
    for s in sites:
        domains = _as_json_obj(s.get("domains"), [])
        result.append({
            "id": s["id"],
            "domains": domains,
            "target": s["target"],
            "redirect": s["redirect"],
            "ssl_forced": bool(s["ssl_forced"]),
            "enabled": bool(s["enabled"]),
            "archived": bool(s.get("archived")),
            "app_id": s["app_id"],
            "source": s["source"],
        })
    return result


@registry.register(
    name="deployra_create_proxy_host",
    description="Create a new reverse proxy host pointing a domain to an internal target IP/port.",
    input_schema={
        "type": "object",
        "properties": {
            "domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of domain names (e.g. ['app.example.com'])",
            },
            "target": {"type": "string", "description": "Target address (e.g. 'http://10.99.0.2:8080')"},
            "ssl_forced": {"type": "boolean", "description": "Force HTTPS redirect", "default": True},
        },
        "required": ["domains", "target"],
    },
    permission=Permission.ALLOW_PROXY,
)
async def _create_proxy_host(panel: Panel, user: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    from . import proxy_state

    domains = [d.strip().lower() for d in args["domains"] if d.strip()]
    if not domains:
        raise ValueError("At least one domain is required")
    target = args["target"].strip()

    site_id = panel.db.insert(
        "sites",
        kind="proxy",
        domains=json.dumps(domains),
        target=target,
        ssl_forced=int(bool(args.get("ssl_forced", True))),
        http2=1,
        websockets=1,
        enabled=1,
        source="mcp",
        created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    # Sync with gateway nginx
    await proxy_state.push(panel)
    return {"site_id": site_id, "domains": domains, "target": target, "status": "created"}


@registry.register(
    name="deployra_list_backups",
    description="List configured backup storage (Local, S3, FTP, SFTP) and existing backup archives.",
    input_schema={"type": "object", "properties": {}, "required": []},
    permission=Permission.ALLOW_SETTINGS,
)
async def _list_backups(panel: Panel, user: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    from .backup_storage import get_backup_storage

    storage = get_backup_storage(panel)
    configured = storage.is_configured()
    backups = []
    if configured:
        try:
            backups = storage.list_backups()
        except Exception as e:
            backups = [{"error": str(e)}]
    return {
        "storage_type": storage.kind,
        "is_configured": configured,
        "backups": backups,
    }


@registry.register(
    name="deployra_search_logs",
    description="Search logs across containers and nginx on all nodes with optional filtering.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search keyword or regex pattern"},
            "source": {"type": "string", "enum": ["all", "nginx", "containers"], "default": "all"},
            "hours": {"type": "integer", "description": "Hours of logs to scan (default: 24)", "default": 24},
            "limit": {"type": "integer", "description": "Max matching lines (default: 50)", "default": 50},
        },
        "required": ["query"],
    },
    permission=Permission.ALLOW_SEEKING_CONTAINER,
)
async def _search_logs(panel: Panel, user: dict[str, Any], args: dict[str, Any]) -> list[dict[str, Any]]:
    from . import logsearch

    q = args["query"].strip()
    source = args.get("source") or "all"
    hours = int(args.get("hours") or 24)
    limit = min(int(args.get("limit") or 50), 200)

    results = await logsearch.search(panel, q, hours=hours, limit=limit, source_filter=source)
    return results


@registry.register(
    name="deployra_list_jobs",
    description="List recent panel jobs (deployment, migration, update, certificate issue) with status and execution logs.",
    input_schema={
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Number of jobs to return (default: 15)", "default": 15},
        },
    },
)
async def _list_jobs(panel: Panel, user: dict[str, Any], args: dict[str, Any]) -> list[dict[str, Any]]:
    limit = min(int(args.get("limit") or 15), 50)
    rows = panel.db.fetchall("SELECT id, kind, title, status, created, finished, error FROM jobs ORDER BY created DESC LIMIT ?", [limit])
    return rows


# --- Główny dispatcher JSON-RPC 2.0 ---


async def dispatch_mcp_message(panel: Panel, user: dict[str, Any], message: dict[str, Any]) -> dict[str, Any] | None:
    """Obsługuje pojedynczy komunikat JSON-RPC 2.0 protokołu MCP."""
    if not isinstance(message, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error: expected JSON object"}}

    msg_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}

    # Obsługa powiadomień bez 'id' (np. notifications/initialized)
    if msg_id is None:
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {},
                },
                "serverInfo": SERVER_INFO,
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "tools/list":
        tools = registry.list_tools(user)
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools}}

    if method == "tools/call":
        tool_name = params.get("name") or ""
        tool_args = params.get("arguments") or {}
        call_res = await registry.call_tool(panel, user, tool_name, tool_args)
        return {"jsonrpc": "2.0", "id": msg_id, "result": call_res}

    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Method '{method}' not implemented."},
    }

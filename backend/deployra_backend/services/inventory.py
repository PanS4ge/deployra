"""Spis oprogramowania na węźle do skanowania podatności — panel sprawdza go w OSV.dev.

Niczego nie uruchamiamy w kontenerach, więc spis działa też na zatrzymanych:
- wersje z metadanych obrazu: zmienne oficjalnych obrazów (`PHP_VERSION`, `NODE_VERSION`, `PG_VERSION`…), także
  odziedziczone — WordPress i Nextcloud stoją na obrazie PHP, Ghost na Node; dla znanych projektów bez takiej
  zmiennej — etykieta `org.opencontainers.image.version` albo dokładny tag (`grafana/grafana:11.2.0`),
- zależności aplikacji „z plików” i z Gita: pliki lock (composer.lock, package-lock.json, poetry.lock…) i to,
  co instalacja naprawdę wgrała (vendor/composer/installed.json, node_modules/.package-lock.json, venv).

Oprogramowanie z obrazów to pakiety ekosystemu „Bitnami” w OSV (php, node, nginx, postgresql…).
"""

from __future__ import annotations

import json
import re
import threading
import tomllib
from pathlib import Path
from typing import Any

from . import docker_svc

BITNAMI = "Bitnami"
SKIP_SYSTEM = ("install", "transfer", "files")  # jednorazowe kontenery Deployry

# zmienna oficjalnego obrazu → pakiet Bitnami w OSV
ENV_PRODUCTS = {
    "PHP_VERSION": "php", "NODE_VERSION": "node", "PYTHON_VERSION": "python", "GOLANG_VERSION": "golang", "RUBY_VERSION": "ruby",
    "NGINX_VERSION": "nginx", "HTTPD_VERSION": "apache", "REDIS_VERSION": "redis", "VALKEY_VERSION": "valkey", "PG_VERSION": "postgresql",
    "MARIADB_VERSION": "mariadb", "MYSQL_VERSION": "mysql", "MONGO_VERSION": "mongodb", "TOMCAT_VERSION": "tomcat", "JAVA_VERSION": "java",
    "DOTNET_VERSION": "dotnet", "ASPNET_VERSION": "aspnet-core", "WORDPRESS_VERSION": "wordpress", "GHOST_VERSION": "ghost",
    "NEXTCLOUD_VERSION": "nextcloud", "MEMCACHED_VERSION": "memcached", "RABBITMQ_VERSION": "rabbitmq", "HAPROXY_VERSION": "haproxy",
    "JENKINS_VERSION": "jenkins", "DRUPAL_VERSION": "drupal", "JOOMLA_VERSION": "joomla", "MEDIAWIKI_VERSION": "mediawiki",
    "REDMINE_VERSION": "redmine", "SOLR_VERSION": "solr", "CASSANDRA_VERSION": "cassandra",
}  # fmt: skip
# nazwa repozytorium obrazu → pakiet (etykieta wersji i tag; także obrazy Bitnami z BITNAMI_APP_NAME)
REPO_PRODUCTS = {
    "php": "php", "php-fpm": "php", "node": "node", "python": "python", "golang": "golang", "ruby": "ruby", "nginx": "nginx",
    "httpd": "apache", "apache": "apache", "redis": "redis", "valkey": "valkey", "postgres": "postgresql", "postgresql": "postgresql",
    "mariadb": "mariadb", "mysql": "mysql", "mongo": "mongodb", "mongodb": "mongodb", "tomcat": "tomcat", "wordpress": "wordpress",
    "ghost": "ghost", "nextcloud": "nextcloud", "memcached": "memcached", "rabbitmq": "rabbitmq", "haproxy": "haproxy",
    "jenkins": "jenkins", "drupal": "drupal", "joomla": "joomla", "mediawiki": "mediawiki", "redmine": "redmine", "solr": "solr",
    "cassandra": "cassandra", "grafana": "grafana", "gitea": "gitea", "keycloak": "keycloak", "prometheus": "prometheus",
    "etcd": "etcd", "nats": "nats", "kafka": "kafka", "mastodon": "mastodon", "discourse": "discourse", "moodle": "moodle",
    "elasticsearch": "elasticsearch", "phpmyadmin": "phpmyadmin", "pgbouncer": "pgbouncer", "zookeeper": "zookeeper",
}  # fmt: skip
TWO_PART = {"postgresql"}  # wersje X.Y są pełne
VERSION = re.compile(r"(\d+(?:\.\d+)+)")

SKIP_DIRS = {".git", "node_modules", "vendor", ".deployra", "data", "__pycache__", ".venv", "venv", "dist", "build", ".next", ".nuxt"}
MAX_FILE = 15 * 1024 * 1024
MAX_PACKAGES = 5000

_images: dict[str, list[dict]] = {}  # ID obrazu → oprogramowanie (obraz o danym ID się nie zmienia)
_lock = threading.Lock()


def clean_version(raw: Any, product: str = "", *, exact: bool = False) -> str | None:
    """`1:11.4.3+maria~ubu2404` → `11.4.3`, `jdk-21.0.4+7` → `21.0.4`; None, gdy nie wiadomo, jaka to dokładnie wersja."""
    text = re.sub(r"^\d+:", "", str(raw or "").strip())  # epoka pakietu Debiana
    text = re.sub(r"^(?:v|jdk-?|jre-?)", "", text, flags=re.IGNORECASE)
    match = VERSION.match(text)
    if not match or (exact and not re.fullmatch(r"[\d.]+(?:-[a-z0-9]+)?", text)):  # tag `16-alpine` to nie wersja
        return None
    parts = match.group(1).split(".")
    if len(parts) < (2 if product in TWO_PART else 3):
        return None
    return ".".join(parts[:4])


def _repo_tag(ref: str) -> tuple[str, str]:
    ref = (ref or "").split("@", 1)[0]
    name, _, tag = ref.rpartition(":") if ":" in ref.rsplit("/", 1)[-1] else (ref, "", "")
    return name.rsplit("/", 1)[-1].lower(), tag


def image_software(info: dict, ref: str = "") -> list[dict]:
    """Oprogramowanie obrazu z jego zmiennych, etykiet i tagu."""
    config = info.get("Config") or {}
    env = dict(item.split("=", 1) for item in config.get("Env") or [] if "=" in item)
    labels = config.get("Labels") or {}
    found: dict[str, dict] = {}
    for var, product in ENV_PRODUCTS.items():
        version = clean_version(env.get(var), product)
        if version:
            found.setdefault(product, {"ecosystem": BITNAMI, "name": product, "version": version, "source": var})
    bitnami = REPO_PRODUCTS.get(str(env.get("BITNAMI_APP_NAME") or "").lower())
    if bitnami and bitnami not in found:
        version = clean_version(env.get("APP_VERSION"), bitnami)
        if version:
            found[bitnami] = {"ecosystem": BITNAMI, "name": bitnami, "version": version, "source": "APP_VERSION"}
    repo, tag = _repo_tag(ref or (info.get("RepoTags") or [""])[0])
    product = REPO_PRODUCTS.get(repo)
    if product and product not in found:
        label = clean_version(labels.get("org.opencontainers.image.version"), product, exact=True)
        version, source = (label, "label") if label else (clean_version(tag, product, exact=True), "tag")
        if version:
            found[product] = {"ecosystem": BITNAMI, "name": product, "version": version, "source": source}
    return list(found.values())


def _image_of(client: Any, image_id: str, ref: str) -> list[dict]:
    key = image_id or ref
    with _lock:
        if key in _images:
            return _images[key]
    try:
        info = client.api.inspect_image(key)
    except Exception:  # noqa: BLE001 — obraz usunięty spod kontenera albo brak uprawnień
        return []
    software = image_software(info, ref)
    with _lock:
        if len(_images) > 512:
            _images.clear()
        _images[key] = software
    return software


def containers(client: Any) -> list[dict]:
    out = []
    for summary in client.api.containers(all=True):
        labels = summary.get("Labels") or {}
        if labels.get(docker_svc.SYSTEM_LABEL) in SKIP_SYSTEM:
            continue
        image = summary.get("Image") or ""
        out.append({
            "id": summary["Id"][:12], "name": (summary.get("Names") or ["/?"])[0].lstrip("/"), "image": image,
            "state": summary.get("State"), "app": labels.get(docker_svc.APP_LABEL), "stack": labels.get("com.docker.compose.project"),
            "system": labels.get(docker_svc.SYSTEM_LABEL), "software": _image_of(client, summary.get("ImageID") or "", image),
        })  # fmt: skip
    return sorted(out, key=lambda c: c["name"])


# --- zależności aplikacji ----------------------------------------------------------------------


def _pypi(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _composer(data: Any) -> list[tuple[str, str]]:
    if isinstance(data, dict):
        dev = set(data.get("dev-package-names") or [])
        items = data.get("packages") or []
    else:  # installed.json z Composera 1
        dev, items = set(), data or []
    return [(p["name"], str(p["version"]).removeprefix("v")) for p in items
            if isinstance(p, dict) and p.get("name") and p.get("version") and p["name"] not in dev
            and not str(p["version"]).startswith("dev-")]  # fmt: skip


def _npm_lock(data: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    packages = data.get("packages")
    if isinstance(packages, dict):  # lockfileVersion 2 i 3
        for path, info in packages.items():
            if path and "node_modules/" in path and isinstance(info, dict) and info.get("version") and not info.get("dev") and not info.get("link"):
                out.append((path.rsplit("node_modules/", 1)[1], str(info["version"])))
        return out

    def walk(deps: dict) -> None:  # lockfileVersion 1
        for name, info in (deps or {}).items():
            if isinstance(info, dict) and info.get("version") and not info.get("dev"):
                out.append((name, str(info["version"])))
                walk(info.get("dependencies") or {})

    walk(data.get("dependencies") or {})
    return out


def _yarn(text: str) -> list[tuple[str, str]]:
    out, names = [], []
    for line in text.splitlines():
        if line and not line.startswith((" ", "#")) and line.rstrip().endswith(":"):
            names = []
            for spec in line.rstrip()[:-1].split(","):
                spec = spec.strip().strip('"')
                name = spec[: spec.rfind("@")] if spec.rfind("@") > 0 else spec
                if name and not name.startswith("__"):
                    names.append(name)
        elif names and line.startswith("  version"):
            version = line.split(None, 1)[1].strip().strip(":").strip().strip('"') if " " in line.strip() else ""
            out += [(name, version) for name in dict.fromkeys(names) if version]
            names = []
    return out


REQ = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*==\s*([^\s;#,]+)")


def _requirements(text: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for line in text.splitlines() if (m := REQ.match(line.strip()))]


def _toml_packages(text: str, skip_local: bool) -> list[tuple[str, str]]:
    out = []
    for p in tomllib.loads(text).get("package") or []:
        if not p.get("name") or not p.get("version"):
            continue
        source = p.get("source")
        if skip_local and (not source or (isinstance(source, dict) and ({"editable", "virtual", "directory", "path"} & set(source)))):
            continue  # sam projekt (Cargo workspace, uv) — nie pakiet z rejestru
        out.append((p["name"], str(p["version"])))
    return out


def _pipfile(data: dict) -> list[tuple[str, str]]:
    return [(name, str(info["version"]).lstrip("=")) for name, info in (data.get("default") or {}).items()
            if isinstance(info, dict) and str(info.get("version") or "").startswith("==")]  # fmt: skip


GO_REQ = re.compile(r"^(?:require\s+)?([A-Za-z0-9.\-_~/]+\.[A-Za-z0-9.\-_~/]+)\s+(v\d[^\s]*)")


def _gomod(text: str) -> list[tuple[str, str]]:
    out, block = [], False
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if line.startswith("require ("):
            block = True
            continue
        if block and line == ")":
            block = False
            continue
        if block or line.startswith("require "):
            m = GO_REQ.match(line)
            if m:
                out.append((m.group(1), m.group(2).removeprefix("v").split("+", 1)[0]))
    return out


def _gemfile(text: str) -> list[tuple[str, str]]:
    out, specs = [], False
    for line in text.splitlines():
        if line.strip() == "specs:":
            specs = True
            continue
        if specs and line and not line.startswith(" "):
            specs = False
        m = re.match(r"^ {4}([A-Za-z0-9_.\-]+) \(([^)]+)\)$", line) if specs else None
        if m:
            out.append((m.group(1), m.group(2).split("-", 1)[0]))
    return out


LOCKFILES: dict[str, tuple[str, Any]] = {
    "composer.lock": ("Packagist", lambda text: _composer(json.loads(text))),
    "package-lock.json": ("npm", lambda text: _npm_lock(json.loads(text))),
    "yarn.lock": ("npm", _yarn),
    "requirements.txt": ("PyPI", _requirements),
    "poetry.lock": ("PyPI", lambda text: _toml_packages(text, skip_local=False)),
    "uv.lock": ("PyPI", lambda text: _toml_packages(text, skip_local=True)),
    "Pipfile.lock": ("PyPI", lambda text: _pipfile(json.loads(text))),
    "go.mod": ("Go", _gomod),
    "Cargo.lock": ("crates.io", lambda text: _toml_packages(text, skip_local=True)),
    "Gemfile.lock": ("RubyGems", _gemfile),
}
INSTALLED = {"vendor/composer/installed.json": "composer.lock", "node_modules/.package-lock.json": "package-lock.json"}


def _read(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _parse(path: Path, kind: str) -> list[tuple[str, str]]:
    text = _read(path)
    if text is None:
        return []
    try:
        return LOCKFILES[kind][1](text)
    except (ValueError, KeyError, TypeError, AttributeError, tomllib.TOMLDecodeError):
        return []  # uszkodzony albo nietypowy plik — pomijamy, reszta spisu zostaje


def _dirs(root: Path, depth: int = 2) -> list[Path]:
    out, level = [root], [root]
    for _ in range(depth):
        nxt = []
        for directory in level:
            try:
                nxt += sorted(p for p in directory.iterdir() if p.is_dir() and p.name not in SKIP_DIRS and not p.is_symlink())
            except OSError:
                continue
        out += nxt[:50]
        level = nxt[:50]
    return out


def app_packages(root: Path) -> list[dict]:
    """Zależności z plików lock w katalogu aplikacji (i dwa poziomy niżej); zainstalowane wygrywają z lockiem."""
    if not root.is_dir():
        return []
    found: dict[tuple[str, str, str], dict] = {}

    def add(ecosystem: str, items: list[tuple[str, str]], source: str) -> None:
        for name, version in items:
            if ecosystem == "PyPI":
                name = _pypi(name)
            key = (ecosystem, name, version)
            if key not in found and len(found) < MAX_PACKAGES:
                found[key] = {"ecosystem": ecosystem, "name": name, "version": version, "source": source}

    for directory in _dirs(root):
        installed: set[str] = set()
        for rel, kind in INSTALLED.items():
            items = _parse(directory / rel, kind)
            if items:
                installed.add(LOCKFILES[kind][0])
                add(LOCKFILES[kind][0], items, (directory / rel).relative_to(root).as_posix())
        if directory == root:
            dist = [(m.group(1), m.group(2)) for p in root.glob(".deployra/venv/lib/python*/site-packages/*.dist-info")
                    if (m := re.match(r"^(.+?)-([^-]+)\.dist-info$", p.name))]  # fmt: skip
            if dist:
                installed.add("PyPI")
                add("PyPI", dist, ".deployra/venv")
        for kind, (ecosystem, _) in LOCKFILES.items():
            if ecosystem not in installed:
                items = _parse(directory / kind, kind)
                if items:
                    add(ecosystem, items, (directory / kind).relative_to(root).as_posix())
    return list(found.values())


def app_roots(ctx: Any) -> dict[str, Path]:
    """Katalogi z kodem aplikacji na tym węźle: „z plików” i z Gita (klon repozytorium)."""
    roots = {}
    for slug, info in (ctx.state.get("apps", {}) or {}).items():
        if info.get("kind") == "runtime":
            roots[slug] = ctx.config.sites_dir / slug
        elif info.get("kind") == "git":
            roots[slug] = ctx.config.builds_dir / slug / "repo"
    return roots


# --- digesty obrazów (aktualizacje) ------------------------------------------------------------


def _digests(info: dict) -> list[str]:
    """`nginx@sha256:…` → `sha256:…` — lista manifestów, z których obraz pobrano (pusta dla zbudowanych lokalnie)."""
    return sorted({d.split("@", 1)[1] for d in info.get("RepoDigests") or [] if "@" in d})


def image_digests(ctx: Any, client: Any) -> list[dict]:
    """Obrazy kontenerów i ich digesty; dla aplikacji „z plików” — obraz języka, na którym zbudowano obraz bazowy
    (etykieta `deployra.base`, a w starszych obrazach — lokalna kopia obrazu języka)."""
    from deployra_common.runtimes import RUNTIMES

    apps = ctx.state.get("apps", {}) or {}
    cache: dict[str, dict] = {}

    def inspect(ref: str) -> dict:
        if ref not in cache:
            try:
                cache[ref] = client.api.inspect_image(ref)
            except Exception:  # noqa: BLE001 — obraz usunięty albo niedostępny
                cache[ref] = {}
        return cache[ref]

    out = []
    for summary in client.api.containers(all=True):
        labels = summary.get("Labels") or {}
        if labels.get(docker_svc.SYSTEM_LABEL):
            continue  # kontenery Deployry (statyczne strony, instalacje) aktualizuje sama Deployra
        slug = labels.get(docker_svc.APP_LABEL)
        info = inspect(summary.get("ImageID") or summary.get("Image") or "")
        ref, digests = summary.get("Image") or "", _digests(info)
        image_labels = (info.get("Config") or {}).get("Labels") or {}
        base = image_labels.get("deployra.base")
        if not base and slug and (apps.get(slug) or {}).get("runtime") in RUNTIMES and not digests:
            base = RUNTIMES[apps[slug]["runtime"]].image  # obraz bazowy sprzed etykiety
        if base:
            ref = base
            digests = [image_labels["deployra.base-digest"]] if image_labels.get("deployra.base-digest") else _digests(inspect(base))
        out.append({"name": (summary.get("Names") or ["/?"])[0].lstrip("/"), "app": slug, "stack": labels.get("com.docker.compose.project"),
                    "image": ref, "digests": digests, "base": bool(base), "state": summary.get("State")})  # fmt: skip
    return sorted(out, key=lambda c: c["name"])


def collect_digests(ctx: Any) -> dict[str, Any]:
    endpoints = []
    for ep in ctx.endpoints.list():
        entry: dict[str, Any] = {"id": ep.id, "name": ep.name, "containers": [], "error": None}
        try:
            entry["containers"] = image_digests(ctx, ctx.docker_for(ep.id))
        except Exception as exc:  # noqa: BLE001
            entry["error"] = str(exc)[:300]
        endpoints.append(entry)
    return {"endpoints": endpoints}


def collect(ctx: Any) -> dict[str, Any]:
    """Wszystkie endpointy (host i CT Proxmoxa) i zależności aplikacji — jeden spis dla panelu."""
    endpoints = []
    for ep in ctx.endpoints.list():
        entry: dict[str, Any] = {"id": ep.id, "name": ep.name, "containers": [], "error": None}
        try:
            entry["containers"] = containers(ctx.docker_for(ep.id))
        except Exception as exc:  # noqa: BLE001 — jeden niedostępny CT nie psuje spisu reszty
            entry["error"] = str(exc)[:300]
        endpoints.append(entry)
    apps = {slug: packages for slug, root in app_roots(ctx).items() if (packages := app_packages(root))}
    return {"endpoints": endpoints, "apps": apps}

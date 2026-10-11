"""Środowiska dla aplikacji „z plików”: PHP, Python, Node, Go, Rust, Ruby, Java, .NET…

Pliki aplikacji leżą na węźle w `/srv/deployra/sites/<slug>` (ZIP z panelu albo SFTP — jak przy
stronach statycznych) i są montowane do kontenera z oficjalnym obrazem języka. Przed startem
jednorazowy kontener instaluje zależności (pip, npm, bundle, go build…) do tego samego katalogu:

- `.deployra/` — pamięć podręczna i wyniki budowania (venv, moduły Go, binarka), przeżywa wgranie nowego ZIP-a,
- `data/` — miejsce na trwałe dane aplikacji (np. SQLite), też przeżywa wgranie ZIP-a.

Polecenia działają przez `sh -c` w katalogu aplikacji; `$PORT` to port, na którym ma słuchać.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

KEEP_DIRS = (".deployra", "data")  # zostają przy podmianie plików z ZIP-a
PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

PY_INSTALL = (
    "if [ -f requirements.txt ]; then "
    "[ -x .deployra/venv/bin/python ] || python -m venv .deployra/venv; "
    ".deployra/venv/bin/pip install --disable-pip-version-check --no-cache-dir -r requirements.txt; fi"
)
NODE_INSTALL = (
    "if [ -f package.json ]; then "
    "if [ -f package-lock.json ]; then npm ci --omit=dev --no-audit --no-fund; else npm install --omit=dev --no-audit --no-fund; fi; fi"
)
NODE_BUILD = (
    "if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi"
    " && npm run build --if-present && npm prune --omit=dev --no-audit --no-fund"
)
PY_ENV = {"PATH": f"/app/.deployra/venv/bin:{PATH}", "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"}
NODE_ENV = {"NODE_ENV": "production", "npm_config_cache": "/app/.deployra/npm", "npm_config_update_notifier": "false"}


@dataclass(frozen=True)
class Runtime:
    id: str
    group: str
    name: str
    icon: str
    image: str  # obraz, w którym działa aplikacja
    start: str  # polecenie startowe (sh -c)
    port: int | None  # domyślny port HTTP; None = proces w tle (bot, worker, skrypt)
    install: str = ""  # instalacja zależności (sh -c) — w `build_image` albo `image`
    build_image: str = ""  # osobny obraz do kompilacji (Go, Rust); aplikacja działa w lżejszym `image`
    base: str = ""  # Dockerfile obrazu bazowego budowanego raz na węźle (np. rozszerzenia PHP)
    mount: str = "/app"
    env: dict[str, str] = field(default_factory=dict)
    memory_mb: int = 256
    live: bool = False  # zmiany plików działają bez restartu
    requires: str = ""  # wzorzec pliku, bez którego nie ma czego uruchomić (np. "*.jar")
    starter: dict[str, str] = field(default_factory=dict)  # pliki świeżej aplikacji

    @property
    def worker(self) -> bool:
        return self.port is None

    @property
    def hint(self) -> str:
        """Krótko, co zostanie uruchomione — do kart w kreatorze."""
        return HINTS.get(self.id, self.start)

    def run_image(self) -> str:
        """Obraz kontenera aplikacji (dla `base` — tag obrazu zbudowanego na węźle)."""
        if self.base:
            import hashlib

            return f"deployra/rt-{self.id}:{hashlib.sha256(self.base.encode()).hexdigest()[:10]}"
        return self.image


HELLO_HTML = "<h1>It works</h1><p>Replace these files (ZIP upload or SFTP) and deploy again.</p>"
Q = json.dumps(HELLO_HTML)  # literał w cudzysłowach — poprawny w Pythonie, JS, Go, Rust i Ruby

RUNTIMES: dict[str, Runtime] = {
    r.id: r
    for r in (
        Runtime(
            id="php", group="PHP", name="PHP + Apache", icon="filetype-php", image="php:8.3-apache",
            base=(
                "FROM php:8.3-apache\n"
                "RUN docker-php-ext-install -j\"$(nproc)\" mysqli pdo_mysql >/dev/null && a2enmod rewrite headers >/dev/null\n"
            ),
            start="chown -R www-data:www-data /var/www/html 2>/dev/null; exec apache2-foreground",
            port=80, mount="/var/www/html", memory_mb=256, live=True,
            starter={"index.php": "<?php $v = PHP_VERSION; ?>\n<!doctype html>\n<meta charset=\"utf-8\">\n<title>PHP <?= $v ?></title>\n"
                                  f"{HELLO_HTML}\n<p>PHP <?= $v ?></p>\n"},
        ),
        Runtime(
            id="python-script", group="Python", name="Python", icon="filetype-py", image="python:3.13-slim",
            install=PY_INSTALL, start="exec python main.py", port=None, env=PY_ENV, memory_mb=128,
            starter={
                "main.py": "import time\n\nwhile True:\n    print(\"alive\", time.strftime(\"%H:%M:%S\"), flush=True)\n    time.sleep(60)\n",
                "requirements.txt": "# one package per line, e.g. requests\n",
            },
        ),
        Runtime(
            id="python-flask", group="Python", name="Flask", icon="filetype-py", image="python:3.13-slim",
            install=PY_INSTALL, start="exec gunicorn --bind 0.0.0.0:$PORT --workers ${WORKERS:-2} --access-logfile - app:app",
            port=8000, env=PY_ENV, memory_mb=192,
            starter={
                "app.py": "from flask import Flask\n\napp = Flask(__name__)\n\n\n@app.get(\"/\")\ndef index():\n"
                          f"    return {Q}\n",
                "requirements.txt": "flask\ngunicorn\n",
            },
        ),
        Runtime(
            id="python-fastapi", group="Python", name="FastAPI", icon="filetype-py", image="python:3.13-slim",
            install=PY_INSTALL, start="exec uvicorn main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips '*'",
            port=8000, env=PY_ENV, memory_mb=192,
            starter={
                "main.py": "from fastapi import FastAPI\nfrom fastapi.responses import HTMLResponse\n\napp = FastAPI()\n\n\n"
                           f"@app.get(\"/\", response_class=HTMLResponse)\ndef index():\n    return {Q}\n",
                "requirements.txt": "fastapi\nuvicorn\n",
            },
        ),
        Runtime(
            id="python-django", group="Python", name="Django", icon="filetype-py", image="python:3.13-slim",
            install=PY_INSTALL,
            start=(
                "mkdir -p data && python manage.py migrate --noinput"
                " && set -- */wsgi.py && exec gunicorn --bind 0.0.0.0:$PORT --workers ${WORKERS:-2} --access-logfile - \"${1%/wsgi.py}.wsgi\""
            ),
            port=8000, env=PY_ENV, memory_mb=256,
            starter={
                "manage.py": "import os\nimport sys\n\nif __name__ == \"__main__\":\n"
                             "    os.environ.setdefault(\"DJANGO_SETTINGS_MODULE\", \"project.settings\")\n"
                             "    from django.core.management import execute_from_command_line\n\n"
                             "    execute_from_command_line(sys.argv)\n",
                "project/__init__.py": "",
                "project/settings.py": "import os\nfrom pathlib import Path\n\nBASE_DIR = Path(__file__).resolve().parent.parent\n"
                                       "SECRET_KEY = os.environ.get(\"DJANGO_SECRET_KEY\", \"change-me\")\n"
                                       "DEBUG = os.environ.get(\"DJANGO_DEBUG\") == \"1\"\nALLOWED_HOSTS = [\"*\"]\n"
                                       "CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get(\"DJANGO_ORIGINS\", \"\").split(\",\") if o]\n"
                                       "SECURE_PROXY_SSL_HEADER = (\"HTTP_X_FORWARDED_PROTO\", \"https\")\n"
                                       "INSTALLED_APPS = [\"django.contrib.contenttypes\", \"django.contrib.auth\"]\n"
                                       "ROOT_URLCONF = \"project.urls\"\nWSGI_APPLICATION = \"project.wsgi.application\"\n"
                                       "DATABASES = {\"default\": {\"ENGINE\": \"django.db.backends.sqlite3\", \"NAME\": BASE_DIR / \"data\" / \"db.sqlite3\"}}\n"
                                       "USE_TZ = True\n",
                "project/urls.py": "from django.http import HttpResponse\nfrom django.urls import path\n\n"
                                   f"urlpatterns = [path(\"\", lambda request: HttpResponse({Q}))]\n",
                "project/wsgi.py": "import os\n\nfrom django.core.wsgi import get_wsgi_application\n\n"
                                   "os.environ.setdefault(\"DJANGO_SETTINGS_MODULE\", \"project.settings\")\napplication = get_wsgi_application()\n",
                "requirements.txt": "django\ngunicorn\n",
            },
        ),
        Runtime(
            id="node-script", group="Node.js", name="Node.js", icon="filetype-js", image="node:22-alpine",
            install=NODE_INSTALL, start="exec node index.js", port=None, env=NODE_ENV, memory_mb=128,
            starter={
                "index.js": "setInterval(() => console.log(\"alive\", new Date().toISOString()), 60_000);\nconsole.log(\"started\");\n",
                "package.json": "{\n  \"name\": \"app\",\n  \"private\": true,\n  \"dependencies\": {}\n}\n",
            },
        ),
        Runtime(
            id="node-web", group="Node.js", name="Express", icon="filetype-js", image="node:22-alpine",
            install=NODE_INSTALL, start="exec npm start --silent", port=3000, env=NODE_ENV, memory_mb=192,
            starter={
                "server.js": "const express = require(\"express\");\n\nconst app = express();\n"
                             f"app.get(\"/\", (req, res) => res.send({Q}));\n"
                             "app.listen(process.env.PORT || 3000, () => console.log(\"listening on\", process.env.PORT || 3000));\n",
                "package.json": "{\n  \"name\": \"app\",\n  \"private\": true,\n  \"scripts\": { \"start\": \"node server.js\" },\n"
                                "  \"dependencies\": { \"express\": \"^4.21.0\" }\n}\n",
            },
        ),
        Runtime(
            id="node-build", group="Node.js", name="Next.js / Nuxt / Astro", icon="filetype-jsx", image="node:22-alpine",
            install=NODE_BUILD, start="exec npm start --silent", port=3000, env={**NODE_ENV, "HOSTNAME": "0.0.0.0"}, memory_mb=512,
            starter={
                "server.js": "const http = require(\"http\");\n\n"
                             f"http.createServer((req, res) => res.end({Q})).listen(process.env.PORT || 3000);\n",
                "package.json": "{\n  \"name\": \"app\",\n  \"private\": true,\n"
                                "  \"scripts\": { \"build\": \"echo build\", \"start\": \"node server.js\" }\n}\n",
            },
        ),
        Runtime(
            id="bun", group="Bun", name="Bun", icon="lightning-charge", image="oven/bun:1-alpine",
            install="if [ -f package.json ]; then bun install --production; fi", start="exec bun run ${MAIN:-index.ts}",
            port=3000, env={"NODE_ENV": "production", "BUN_INSTALL_CACHE_DIR": "/app/.deployra/bun"}, memory_mb=192,
            starter={"index.ts": "Bun.serve({\n  port: Number(process.env.PORT ?? 3000),\n"
                                 f"  fetch: () => new Response({Q}, {{ headers: {{ \"content-type\": \"text/html\" }} }}),\n}});\n"},
        ),
        Runtime(
            id="deno", group="Deno", name="Deno", icon="filetype-tsx", image="denoland/deno:alpine",
            install="deno cache ${MAIN:-main.ts}", start="exec deno run --allow-net --allow-env --allow-read --allow-write=data ${MAIN:-main.ts}",
            port=8000, env={"DENO_DIR": "/app/.deployra/deno"}, memory_mb=192,
            starter={"main.ts": "Deno.serve({ port: Number(Deno.env.get(\"PORT\") ?? 8000) }, () =>\n"
                                f"  new Response({Q}, {{ headers: {{ \"content-type\": \"text/html\" }} }}));\n"},
        ),
        Runtime(
            id="go", group="Go", name="Go", icon="code-slash", image="alpine:3.20", build_image="golang:1.23-alpine",
            install="CGO_ENABLED=0 go build -trimpath -o .deployra/app .", start="exec ./.deployra/app", port=8080,
            env={"GOCACHE": "/app/.deployra/go/cache", "GOMODCACHE": "/app/.deployra/go/mod", "GOFLAGS": "-buildvcs=false"}, memory_mb=128,
            starter={
                "go.mod": "module app\n\ngo 1.23\n",
                "main.go": "package main\n\nimport (\n\t\"fmt\"\n\t\"net/http\"\n\t\"os\"\n)\n\nfunc main() {\n"
                           "\thttp.HandleFunc(\"/\", func(w http.ResponseWriter, r *http.Request) {\n"
                           f"\t\tfmt.Fprint(w, {Q})\n\t}})\n"
                           "\tport := os.Getenv(\"PORT\")\n\tif port == \"\" {\n\t\tport = \"8080\"\n\t}\n"
                           "\thttp.ListenAndServe(\":\"+port, nil)\n}\n",
            },
        ),
        Runtime(
            id="rust", group="Rust", name="Rust", icon="gear-wide-connected", image="debian:bookworm-slim", build_image="rust:1-slim-bookworm",
            install="cargo install --path . --root .deployra/rust --force", start="set -- .deployra/rust/bin/*; exec \"$1\"", port=8080,
            env={"CARGO_HOME": "/app/.deployra/cargo", "CARGO_TARGET_DIR": "/app/.deployra/target"}, memory_mb=128,
            starter={
                "Cargo.toml": "[package]\nname = \"app\"\nversion = \"0.1.0\"\nedition = \"2021\"\n\n[dependencies]\n",
                "src/main.rs": "use std::io::{Read, Write};\nuse std::net::TcpListener;\n\nfn main() {\n"
                               "    let port = std::env::var(\"PORT\").unwrap_or_else(|_| \"8080\".into());\n"
                               "    let listener = TcpListener::bind(format!(\"0.0.0.0:{port}\")).unwrap();\n"
                               "    for stream in listener.incoming().flatten() {\n        let mut stream = stream;\n"
                               "        let mut buf = [0u8; 1024];\n        let _ = stream.read(&mut buf);\n"
                               f"        let body = {Q};\n"
                               "        let _ = write!(stream, \"HTTP/1.1 200 OK\\r\\ncontent-type: text/html\\r\\ncontent-length: {}\\r\\n\\r\\n{}\", body.len(), body);\n"
                               "    }\n}\n",
            },
        ),
        Runtime(
            id="ruby", group="Ruby", name="Ruby (Rack / Sinatra)", icon="gem", image="ruby:3.3-slim",
            install="if [ -f Gemfile ]; then bundle install; fi", start="exec bundle exec rackup --host 0.0.0.0 --port $PORT",
            port=9292, env={"BUNDLE_PATH": "/app/.deployra/bundle", "BUNDLE_WITHOUT": "development:test", "RACK_ENV": "production"},
            memory_mb=192,
            starter={
                "config.ru": f"run ->(env) {{ [200, {{ \"content-type\" => \"text/html\" }}, [{Q}]] }}\n",
                "Gemfile": "source \"https://rubygems.org\"\n\ngem \"rack\"\ngem \"rackup\"\ngem \"webrick\"\n",
            },
        ),
        Runtime(
            id="java", group="Java", name="Java (.jar)", icon="cup-hot", image="eclipse-temurin:21-jre-alpine",
            start="set -- *.jar; exec java ${JAVA_OPTS:--XX:MaxRAMPercentage=75} -jar \"$1\"", port=8080,
            env={"SERVER_PORT": "8080"}, memory_mb=384, requires="*.jar",
        ),
        Runtime(
            id="dotnet", group=".NET", name=".NET (ASP.NET)", icon="windows", image="mcr.microsoft.com/dotnet/aspnet:8.0",
            start="set -- *.runtimeconfig.json; exec dotnet \"${1%.runtimeconfig.json}.dll\"", port=8080,
            env={"ASPNETCORE_HTTP_PORTS": "8080", "DOTNET_CLI_TELEMETRY_OPTOUT": "1"}, memory_mb=256, requires="*.runtimeconfig.json",
        ),
        Runtime(
            id="shell", group="Shell", name="Shell", icon="terminal", image="alpine:3.20",
            start="exec sh run.sh", port=None, memory_mb=32,
            starter={"run.sh": "#!/bin/sh\nwhile true; do\n  echo \"alive $(date +%H:%M:%S)\"\n  sleep 60\ndone\n"},
        ),
    )
}  # fmt: skip

HINTS = {
    "php": "index.php · .htaccess",
    "python-script": "python main.py",
    "python-flask": "gunicorn app:app",
    "python-fastapi": "uvicorn main:app",
    "python-django": "gunicorn project.wsgi",
    "node-script": "node index.js",
    "node-web": "npm start",
    "node-build": "npm run build · npm start",
    "bun": "bun run index.ts",
    "deno": "deno run main.ts",
    "go": "go build",
    "rust": "cargo build",
    "ruby": "rackup config.ru",
    "java": "java -jar *.jar",
    "dotnet": "dotnet App.dll",
    "shell": "sh run.sh",
}


def get(runtime_id: str | None) -> Runtime | None:
    return RUNTIMES.get(runtime_id or "")


def env_for(runtime: Runtime, port: int | None, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {**runtime.env}
    if port:
        env["PORT"] = str(port)
    return {**env, **(extra or {})}

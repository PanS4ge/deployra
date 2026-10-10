"""Środowisko deweloperskie: Deployra-WG + backend (atrapa Dockera) + panel na localhoście.

    python scripts/dev_stack.py            # uruchom (dane w .dev/)
    python scripts/dev_stack.py --seed     # + konfiguracja i przykładowe dane
    python scripts/dev_stack.py --reset    # wyczyść .dev/ i zacznij od zera

Bez WireGuarda, bez nginx i bez prawdziwego Dockera — działa także na Windowsie.
Panel: http://127.0.0.1:8080 (przy --seed: admin@deployra.local / kosmos-demo).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DEV = ROOT / ".dev"
PANEL = "http://127.0.0.1:8080"
ADMIN = ("admin@deployra.local", "kosmos-demo")
DEMO_DOMAINS = {"blog.example.com": 1180, "chmura.example.com": 704, "grafana.example.com": 4030, "festiwal.example.com": 1060}

SERVICES = {
    "gateway": ("deployra_gateway", ROOT / "gateway", {
        "DEPLOYRA_DATA": DEV / "gateway", "DEPLOYRA_PUBLIC_HOST": "127.0.0.1", "DEPLOYRA_WG": "off",
        "DEPLOYRA_NGINX_RELOAD": "off", "DEPLOYRA_API_BIND": "127.0.0.1", "DEPLOYRA_NGINX_DIR": DEV / "gateway" / "nginx",
        "DEPLOYRA_CERTS_DIR": DEV / "gateway" / "certs", "DEPLOYRA_ACME_DIR": DEV / "gateway" / "acme", "DEPLOYRA_IPV6": "off",
    }),
    "backend": ("deployra_backend", ROOT / "backend", {
        "DEPLOYRA_DATA": DEV / "node", "DEPLOYRA_SRV": DEV / "node" / "srv", "DEPLOYRA_FAKE_DOCKER": "1",
        "DEPLOYRA_API_BIND": "127.0.0.1", "DEPLOYRA_WG": "off", "DEPLOYRA_NAME": "dom-demo", "DEPLOYRA_PROXMOX": "off",
        "DEPLOYRA_SFTP_PORT": "2222",
    }),
    "panel": ("deployra_panel", ROOT / "frontend", {
        "DEPLOYRA_DATA": DEV / "panel", "DEPLOYRA_WG": "off", "DEPLOYRA_PORT": "8080", "DEPLOYRA_BIND": "127.0.0.1",
    }),
}  # fmt: skip


def start(name: str) -> subprocess.Popen:
    module, cwd, extra = SERVICES[name]
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONPATH": str(ROOT / "shared"), **{k: str(v) for k, v in extra.items()}}
    log = open(DEV / f"{name}.log", "a", encoding="utf-8")  # noqa: SIM115 — plik żyje tyle co proces
    return subprocess.Popen([sys.executable, "-m", module], cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT)


def wait_for(url: str, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(url, timeout=2)
            return
        except httpx.HTTPError:
            time.sleep(0.4)
    raise SystemExit(f"{url} nie wstał — sprawdź logi w .dev/*.log")


def seed_analytics() -> None:
    """Przykładowe statystyki odwiedzin z 30 dni (zanim bramka wystartuje)."""
    path = DEV / "gateway" / "analytics.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE IF NOT EXISTS daily (domain TEXT NOT NULL, day TEXT NOT NULL, requests INTEGER NOT NULL DEFAULT 0,"
        " uniques INTEGER NOT NULL DEFAULT 0, returners INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,"
        " PRIMARY KEY (domain, day));"
        "CREATE TABLE IF NOT EXISTS hourly (domain TEXT NOT NULL, hour INTEGER NOT NULL, requests INTEGER NOT NULL DEFAULT 0,"
        " uniques INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (domain, hour));"
    )
    today = datetime.now(timezone.utc).date()
    rng = random.Random(42)
    for domain, monthly in DEMO_DOMAINS.items():
        base = monthly / 30
        for i in range(30):
            day = (today - timedelta(days=29 - i)).isoformat()
            uniques = max(1, int(base * rng.uniform(0.6, 1.5) * (1.6 if i in (11, 23) else 1)))
            returning = int(uniques * rng.uniform(0.2, 0.45))
            requests = uniques * rng.randint(6, 14)
            db.execute("INSERT OR REPLACE INTO daily VALUES (?, ?, ?, ?, ?, ?)", (domain, day, requests, uniques, returning, requests * 24_000))
        hour_now = int(time.time() // 3600)
        for h in range(hour_now - 23, hour_now + 1):
            local_hour = datetime.fromtimestamp(h * 3600).hour
            weight = 0.3 + (1.0 if 8 <= local_hour <= 23 else 0.2)
            uniques = max(0, int(base / 18 * weight * rng.uniform(0.5, 1.4)))
            db.execute("INSERT OR REPLACE INTO hourly VALUES (?, ?, ?, ?)", (domain, h, uniques * rng.randint(5, 12), uniques))
    db.commit()
    db.close()


def seed_panel() -> None:
    gw_code = json.loads((DEV / "gateway" / "state.json").read_text(encoding="utf-8"))["pairing_code"]
    node_code = json.loads((DEV / "node" / "state.json").read_text(encoding="utf-8"))["pairing_code"]
    with httpx.Client(base_url=PANEL, timeout=60, headers={"HX-Request": "true"}) as c:
        c.post("/setup", data={"name": "Ada", "surname": "Kosmiczna", "email": ADMIN[0], "password": ADMIN[1], "password_repeat": ADMIN[1],
                               "base_domain": "example.com", "panel_domain": "deploy.example.com"})  # fmt: skip
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/settings", headers={"HX-Request": ""}).text).group(1)
        c.headers["X-CSRF-Token"] = csrf

        def post(path: str, data: dict) -> httpx.Response:
            response = c.post(path, data=data)
            if response.status_code >= 400:
                raise SystemExit(f"{path}: {response.status_code} {response.headers.get('HX-Trigger', '')}")
            return response

        post("/tunnel/pair", {"address": "127.0.0.1:51821", "code": gw_code, "colocated": "1", "api_override": "http://127.0.0.1:2751"})
        post("/nodes/direct", {"name": "dom-demo", "url": "http://127.0.0.1:2750", "code": node_code, "proxy_address": "127.0.0.1"})
        node_id = 1
        for name, image, port, domain in (
            ("Blog", "ghost:5-alpine", 2368, "blog.example.com"),
            ("Grafana", "grafana/grafana:11.2.0", 3000, "grafana.example.com"),
        ):
            post("/apps/new", {"kind": "image", "name": name, "slug": name.lower(), "node_id": node_id, "endpoint": "local",
                               "image": image, "container_port": port, "domains": domain})  # fmt: skip
        post("/proxy/hosts/save", {"kind": "proxy", "domains": "chmura.example.com", "target_type": "address", "host": "192.168.1.50",
                                   "port": "443", "scheme": "https", "websockets": "1", "block_exploits": "1", "enabled": "1",
                                   "http2": "1"})  # fmt: skip
        post("/proxy/hosts/save", {"kind": "redirect", "domains": "festiwal.example.com", "redirect_domain": "blog.example.com",
                                   "redirect_code": "301", "redirect_scheme": "https", "preserve_path": "1", "enabled": "1"})  # fmt: skip
        post("/uptime/url", {"name": "Strona główna", "url": "https://example.com", "interval": "60"})
    print(f"✔ Dane przykładowe gotowe — zaloguj się: {ADMIN[0]} / {ADMIN[1]}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # konsola Windowsa (cp1250) nie zna np. „✔”
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", action="store_true", help="skonfiguruj panel i dodaj przykładowe dane")
    parser.add_argument("--reset", action="store_true", help="usuń .dev/ przed startem")
    args = parser.parse_args()
    if args.reset and DEV.exists():
        shutil.rmtree(DEV)
    fresh = not (DEV / "panel").exists()
    DEV.mkdir(exist_ok=True)
    if args.seed and fresh:
        seed_analytics()
    procs = {name: start(name) for name in SERVICES}

    def stop(*_: object) -> None:
        for proc in procs.values():
            proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        wait_for("http://127.0.0.1:51821/health")
        wait_for("http://127.0.0.1:2750/health")
        wait_for(f"{PANEL}/healthz")
        if args.seed and fresh:
            seed_panel()
        print(f"Deployra dev: panel {PANEL} · bramka 127.0.0.1:51821/2751 · węzeł 127.0.0.1:2750 · logi w .dev/*.log")
        while True:
            for name, proc in procs.items():
                if proc.poll() is not None:
                    print(f"{name} zakończył się (kod {proc.returncode}) — zobacz .dev/{name}.log")
                    stop()
            time.sleep(1)
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.terminate()


if __name__ == "__main__":
    main()

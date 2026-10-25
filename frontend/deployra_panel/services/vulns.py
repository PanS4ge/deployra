"""Podatności (CVE) w oprogramowaniu na węzłach: spis z węzłów → OSV.dev → ostrzeżenia, alerty i naprawa.

Węzeł podaje, co działa w kontenerach (wersje z metadanych obrazów: PHP, Node, nginx, PostgreSQL… — w OSV to
pakiety ekosystemu „Bitnami”) i jakie zależności mają aplikacje „z plików” i z Gita (composer.lock, package-lock.json…).
Panel sprawdza to w OSV.dev (`querybatch`, bez klucza API) i dociąga szczegóły nowych wpisów (`vuln_cache`).
Lista CISA KEV (raz na dobę) oznacza podatności, które ktoś już wykorzystuje w atakach.

Wynik leży w `vuln_findings`: wiersz to jedna paczka w jednym kontenerze albo pliku lock aplikacji, z listą
podatności, najwyższą wagą i wersją, w której wszystkie znane dziury są załatane. Paczka, której nie ma już
w kolejnym spisie (albo ma wersję bez podatności), przechodzi w „resolved”; „ignored” wraca do „open”, gdy
pojawi się nowa podatność.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger

from deployra_common import i18n as i18n_core
from deployra_common.client import RemoteError

from .. import __version__
from ..i18n import t

OSV = "https://api.osv.dev/v1"
KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
SEVERITIES = ("critical", "high", "medium", "low", "unknown")
RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "unknown": 0}
LEVELS = {"critical": "critical", "high": "high", "important": "high", "moderate": "medium", "medium": "medium", "low": "low"}
DEFAULTS = {"enabled": True, "interval_hours": 6, "alert_severity": "high"}
INTERVALS = (1, 3, 6, 12, 24)
FIRST_SCAN = 180
TICK = 600
KEV_EVERY = 24 * 3600
BATCH = 1000  # limit zapytań w jednym querybatch
PARALLEL = 8
MAX_DETAILS = 3000  # tyle nowych wpisów OSV dociągamy w jednym skanie (reszta — w następnym)
INVENTORY_TIMEOUT = 90
KEEP = 30 * 86400
RETRIES = 3
RETRY_DELAY = 2.0  # sekundy (rośnie z każdą próbą)
RETRY_FAILED = 1800  # po nieudanym skanie (OSV albo sieć) — kolejna próba po 30 min, nie po pełnym interwale
RUNNING: set[int] = set()  # id(panel) w trakcie skanowania


# --- ustawienia i stan -------------------------------------------------------------------------


def settings(panel: Any) -> dict[str, Any]:
    return {**DEFAULTS, **(panel.db.setting("vuln_settings") or {})}


def save_settings(panel: Any, enabled: bool, interval_hours: int, alert_severity: str) -> None:
    if interval_hours not in INTERVALS or alert_severity not in SEVERITIES[:4]:
        raise ValueError(t("vuln.bad_settings"))
    panel.db.set_setting("vuln_settings", {"enabled": enabled, "interval_hours": interval_hours, "alert_severity": alert_severity})


def status(panel: Any) -> dict[str, Any]:
    return panel.db.setting("vuln_status") or {}


def running(panel: Any) -> bool:
    return id(panel) in RUNNING


# --- wersje, waga, poprawka ---------------------------------------------------------------------

_PRE = re.compile(r"^[-.~_+]?(dev|a|alpha|b|beta|c|pre|preview|rc)\.?\d*", re.IGNORECASE)


def vkey(version: Any) -> tuple:
    """Klucz porównania wersji (SemVer, PHP, PEP 440 w uproszczeniu): 8.4.0RC1 < 8.4.0 == 8.4 < 8.4.1."""
    text = str(version or "").strip().removeprefix("v")
    match = re.match(r"^(\d+(?:\.\d+)*)(.*)$", text)
    if not match:
        return ((), 0, text)
    nums = tuple(int(part) for part in match.group(1).split("."))
    while len(nums) > 1 and nums[-1] == 0:
        nums = nums[:-1]
    rest = match.group(2)
    pre = _PRE.match(rest)
    return (nums, 0, rest.lower()) if pre else (nums, 1, "")


def _norm(ecosystem: str, name: str) -> str:
    if ecosystem == "PyPI":
        return re.sub(r"[-_.]+", "-", name).lower()
    return name.lower() if ecosystem in ("Packagist", "Bitnami") else name


_CVSS3 = {"AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}, "AC": {"L": 0.77, "H": 0.44}, "UI": {"N": 0.85, "R": 0.62},
          "CIA": {"H": 0.56, "L": 0.22, "N": 0.0}}  # fmt: skip


def cvss3(vector: Any) -> float | None:
    """Wynik bazowy CVSS 3.x z wektora (wzór ze specyfikacji FIRST); None dla innych wersji."""
    text = str(vector or "")
    if not text.startswith("CVSS:3"):
        return None
    metrics = dict(part.split(":", 1) for part in text.split("/")[1:] if ":" in part)
    try:
        changed = metrics["S"] == "C"
        pr = {"N": 0.85, "L": 0.68 if changed else 0.62, "H": 0.5 if changed else 0.27}[metrics["PR"]]
        iss = 1 - math.prod(1 - _CVSS3["CIA"][metrics[k]] for k in ("C", "I", "A"))
        exploit = 8.22 * _CVSS3["AV"][metrics["AV"]] * _CVSS3["AC"][metrics["AC"]] * pr * _CVSS3["UI"][metrics["UI"]]
    except KeyError:
        return None
    impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if changed else 6.42 * iss
    if impact <= 0:
        return 0.0
    raw = min((1.08 if changed else 1.0) * (impact + exploit), 10)
    scaled = round(raw * 100000)
    return scaled / 100000 if scaled % 10000 == 0 else (math.floor(scaled / 10000) + 1) / 10


def bucket(score: float) -> str:
    return "critical" if score >= 9 else "high" if score >= 7 else "medium" if score >= 4 else "low" if score > 0 else "unknown"


def rate(record: dict) -> tuple[str, float | None]:
    """Waga z bazy źródłowej (GHSA, Bitnami), a gdy jej nie ma — z wektora CVSS 3.x."""
    vectors = [s.get("score") for s in record.get("severity") or []]
    for affected in record.get("affected") or []:
        vectors += [s.get("score") for s in affected.get("severity") or []]
    scores = [score for score in map(cvss3, vectors) if score is not None]
    score = max(scores) if scores else None
    label = LEVELS.get(str((record.get("database_specific") or {}).get("severity") or "").lower())
    return label or (bucket(score) if score is not None else "unknown"), score


def compact(record: dict) -> dict:
    """Wpis OSV skrócony do tego, czego potrzebuje panel (ID, CVE, waga, zakresy wersji)."""
    severity, score = rate(record)
    cves = sorted({a for a in [record.get("id", ""), *(record.get("aliases") or [])] if a.startswith("CVE-")})
    affected = []
    for item in record.get("affected") or []:
        package = item.get("package") or {}
        ranges = [[[kind, value] for event in r.get("events") or [] for kind, value in event.items()]
                  for r in item.get("ranges") or [] if r.get("type") in ("SEMVER", "ECOSYSTEM")]  # fmt: skip
        affected.append({"ecosystem": package.get("ecosystem"), "name": package.get("name"), "ranges": ranges})
    summary = (record.get("summary") or (record.get("details") or "").strip().split("\n", 1)[0]).strip()
    advisory = next((r["url"] for r in record.get("references") or [] if r.get("type") == "ADVISORY" and r.get("url")), "")
    return {"id": record["id"], "modified": record.get("modified"), "published": record.get("published"), "summary": summary[:300],
            "cves": cves, "severity": severity, "score": score, "affected": affected, "url": advisory}  # fmt: skip


def fix_for(record: dict, ecosystem: str, name: str, version: str) -> str | None:
    """Wersja z poprawką w gałęzi, do której należy `version`; "" — podatna i bez poprawki; None — nie wiadomo."""
    current, answer = vkey(version), None
    for item in record.get("affected") or []:
        if item.get("ecosystem") != ecosystem or _norm(ecosystem, item.get("name") or "") != _norm(ecosystem, name):
            continue
        for events in item.get("ranges") or []:
            start = None
            for kind, value in events:
                if kind == "introduced":
                    start = vkey(value)
                elif start is not None and kind in ("fixed", "last_affected"):
                    end = vkey(value)
                    if start <= current and (current < end if kind == "fixed" else current <= end):
                        return value if kind == "fixed" else ""
                    start = None
            if start is not None and start <= current:  # zakres bez końca — poprawki jeszcze nie ma
                answer = ""
    return answer


# --- spis z węzłów -------------------------------------------------------------------------------


@dataclass
class Target:
    node_id: int
    endpoint: str
    container: str  # "" — zależności aplikacji z pliku lock
    app_id: int | None
    label: str  # nazwa aplikacji albo kontenera
    ecosystem: str
    package: str
    version: str
    source: str

    @property
    def key(self) -> tuple:
        # plik lock może mieć tę samą paczkę w kilku wersjach (zagnieżdżone node_modules) — wtedy wersja jest częścią klucza;
        # w kontenerze produkt jest jeden, więc nowa wersja aktualizuje ten sam wiersz (i może go zamknąć)
        return (self.node_id, self.endpoint, self.container, self.app_id or 0, self.ecosystem, self.package, self.source,
                "" if self.container else self.version)  # fmt: skip


def _row_key(row: dict) -> tuple:
    return (row["node_id"], row["endpoint"], row["container"], row["app_id"] or 0, row["ecosystem"], row["package"], row["source"] or "",
            "" if row["container"] else row["version"])  # fmt: skip


async def _inventory(panel: Any, node: dict) -> tuple[dict | None, str | None]:
    try:
        data = await asyncio.wait_for(panel.node_client(node).get("/api/security/inventory", timeout=INVENTORY_TIMEOUT), INVENTORY_TIMEOUT + 5)
        return data, None
    except RemoteError as exc:
        return None, t("vuln.node_too_old") if exc.status in (404, 405) else exc.detail
    except (httpx.HTTPError, asyncio.TimeoutError, OSError, LookupError):
        return None, t("vuln.node_offline")


def _endpoint(panel: Any, app: dict) -> str:
    from .deploy import load

    try:
        return load(panel, app)[0].endpoint
    except Exception:  # noqa: BLE001 — uszkodzona specyfikacja nie psuje skanu
        return "local"


def targets(panel: Any, node: dict, inventory: dict) -> list[Target]:
    """Oprogramowanie z kontenerów (przypisane do aplikacji panelu, gdy da się ją rozpoznać) i zależności aplikacji."""
    from .uptime import app_containers

    apps = panel.db.fetchall("SELECT * FROM apps WHERE node_id = ?", [node["id"]])
    endpoints = {app["id"]: _endpoint(panel, app) for app in apps}
    out: list[Target] = []
    for ep in inventory.get("endpoints") or []:
        items = [c for c in ep.get("containers") or [] if c.get("name")]
        owner: dict[str, dict] = {}
        for app in apps:
            if endpoints[app["id"]] == ep.get("id"):
                for container in app_containers(items, app):
                    owner.setdefault(container["name"], app)
        for container in items:
            app = owner.get(container["name"])
            for sw in container.get("software") or []:
                out.append(Target(node["id"], ep["id"], container["name"], app["id"] if app else None, app["name"] if app else container["name"],
                                  sw["ecosystem"], sw["name"], str(sw["version"]), sw.get("source") or ""))  # fmt: skip
    by_slug = {app["slug"]: app for app in apps}
    for slug, packages in (inventory.get("apps") or {}).items():
        app = by_slug.get(slug)
        if app:
            out += [Target(node["id"], endpoints[app["id"]], "", app["id"], app["name"], p["ecosystem"], p["name"], str(p["version"]),
                           p.get("source") or "") for p in packages]  # fmt: skip
    return out


# --- OSV.dev i CISA KEV --------------------------------------------------------------------------


async def kev(panel: Any, http: httpx.AsyncClient) -> set[str]:
    """CVE z katalogu CISA Known Exploited Vulnerabilities (odświeżane raz na dobę)."""
    stored = panel.db.setting("vuln_kev") or {}
    if stored.get("cves") and time.time() - float(stored.get("fetched_at") or 0) < KEV_EVERY:
        return set(stored["cves"])
    try:
        response = await _send(http, "GET", KEV_URL, timeout=60)
        cves = sorted({str(v["cveID"]) for v in response.json().get("vulnerabilities") or [] if v.get("cveID")})
        panel.db.set_setting("vuln_kev", {"fetched_at": time.time(), "cves": cves})
        return set(cves)
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        logger.warning(f"CISA KEV: {exc}")
        return set(stored.get("cves") or [])


async def _send(http: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
    """Zapytanie z ponowieniem przy chwilowych błędach (DNS, zerwane połączenie, 429/5xx) — słabe łącza VPS-ów."""
    for attempt in range(RETRIES):
        try:
            response = await http.request(method, url, **kwargs)
            if response.status_code in (429, 500, 502, 503, 504) and attempt < RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                continue
            response.raise_for_status()
            return response
        except httpx.TransportError:
            if attempt == RETRIES - 1:
                raise
            await asyncio.sleep(RETRY_DELAY * (attempt + 1))
    raise AssertionError("unreachable")


def _query(key: tuple[str, str, str]) -> dict:
    return {"package": {"ecosystem": key[0], "name": key[1]}, "version": key[2]}


async def query(http: httpx.AsyncClient, keys: list[tuple[str, str, str]]) -> dict[tuple[str, str, str], list[dict]]:
    """(ekosystem, paczka, wersja) → [{id, modified}] — podatności dotyczące dokładnie tej wersji."""
    out: dict[tuple[str, str, str], list[dict]] = {}
    for start in range(0, len(keys), BATCH):
        chunk = keys[start : start + BATCH]
        response = await _send(http, "POST", f"{OSV}/querybatch", json={"queries": [_query(k) for k in chunk]})
        for key, result in zip(chunk, response.json().get("results") or []):
            found = list(result.get("vulns") or [])
            token, pages = result.get("next_page_token"), 0
            while token and pages < 10:  # bardzo stara wersja z setkami podatności — kolejne strony
                more = await _send(http, "POST", f"{OSV}/query", json={**_query(key), "page_token": token})
                data = more.json()
                found += [{"id": v["id"], "modified": v.get("modified")} for v in data.get("vulns") or []]
                token, pages = data.get("next_page_token"), pages + 1
            out[key] = found
    return out


def _cache_put(panel: Any, record: dict) -> None:
    params = [record["id"], record.get("modified"), json.dumps(record, ensure_ascii=False), time.time()]
    if panel.db.dialect == "sqlite":
        sql = ("INSERT INTO vuln_cache (id, modified, data, fetched_at) VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
               "modified = excluded.modified, data = excluded.data, fetched_at = excluded.fetched_at")  # fmt: skip
    else:
        sql = ("INSERT INTO vuln_cache (id, modified, data, fetched_at) VALUES (?, ?, ?, ?) ON DUPLICATE KEY UPDATE "
               "modified = VALUES(modified), data = VALUES(data), fetched_at = VALUES(fetched_at)")  # fmt: skip
    panel.db.execute(sql, params)


async def details(panel: Any, http: httpx.AsyncClient, wanted: dict[str, str | None]) -> dict[str, dict]:
    """ID → skrócony wpis: z pamięci podręcznej, gdy OSV go nie zmieniał, inaczej z `/v1/vulns/{id}`."""
    cached = {row["id"]: row for row in panel.db.fetchall("SELECT id, modified, data, fetched_at FROM vuln_cache")}
    out: dict[str, dict] = {}
    missing = []
    for vid, modified in wanted.items():
        row = cached.get(vid)
        if row and (not modified or row["modified"] == modified):
            out[vid] = json.loads(row["data"])
        else:
            missing.append(vid)
    gate = asyncio.Semaphore(PARALLEL)

    async def fetch(vid: str) -> dict:
        async with gate:
            response = await _send(http, "GET", f"{OSV}/vulns/{vid}")
            return compact(response.json())

    results = await asyncio.gather(*(fetch(vid) for vid in missing[:MAX_DETAILS]), return_exceptions=True)
    for vid, result in zip(missing, results):
        if isinstance(result, BaseException):
            if vid in cached:  # nieaktualny wpis jest lepszy niż żaden
                out[vid] = json.loads(cached[vid]["data"])
            continue
        out[vid] = result
        _cache_put(panel, result)
    stale = [vid for vid, row in cached.items() if vid not in wanted and row["fetched_at"] < time.time() - KEEP]
    for start in range(0, len(stale), 200):  # wpisy, których żaden skan już nie potrzebuje, znikają po miesiącu
        chunk = stale[start : start + 200]
        panel.db.execute(f"DELETE FROM vuln_cache WHERE id IN ({', '.join('?' for _ in chunk)})", chunk)
    return out


# --- ocena i zapis -------------------------------------------------------------------------------


def evaluate(items: list[Target], results: dict, records: dict[str, dict], exploited: set[str]) -> dict[tuple, dict]:
    """Podatności każdej paczki (bez duplikatów: GHSA i PYSEC to często ten sam CVE) i wersja z poprawkami."""
    findings: dict[tuple, dict] = {}
    for target in items:
        entries: dict[str, dict] = {}
        for ref in results.get((target.ecosystem, target.package, target.version)) or []:
            # szczegółów nie udało się pobrać (sieć, limit na skan) — podatność i tak jest, dociągniemy ją następnym razem
            record = records.get(ref["id"]) or {"id": ref["id"], "cves": [ref["id"]] if ref["id"].startswith("CVE-") else [], "severity": "unknown",
                                                "score": None, "summary": "", "affected": []}  # fmt: skip
            cve = record["cves"][0] if record["cves"] else ""
            entry = {"id": record["id"], "cve": cve, "severity": record["severity"], "score": record["score"], "summary": record["summary"],
                     "fixed": fix_for(record, target.ecosystem, target.package, target.version), "kev": bool(set(record["cves"]) & exploited),
                     "published": record.get("published"), "url": record.get("url") or ""}  # fmt: skip
            key = cve or record["id"]
            previous = entries.get(key)
            if not previous or (RANK[entry["severity"]], entry["fixed"] is not None) > (RANK[previous["severity"]], previous["fixed"] is not None):
                entries[key] = entry
        if not entries:
            continue
        vulns = sorted(entries.values(), key=lambda e: (-e["kev"], -RANK[e["severity"]], -(e["score"] or 0), e["cve"] or e["id"]))
        fixes = [e["fixed"] for e in vulns if e["fixed"]]
        findings[target.key] = {
            "target": target, "vulns": vulns, "severity": max((e["severity"] for e in vulns), key=RANK.__getitem__),
            "score": max((e["score"] or 0 for e in vulns), default=0) or None, "kev": any(e["kev"] for e in vulns),
            "fixed": max(fixes, key=vkey) if fixes else None,
        }  # fmt: skip
    return findings


def store(panel: Any, findings: dict[tuple, dict], covered: set[tuple[int, str]], scanned: set[int]) -> list[tuple[str, dict]]:
    """Zapis wyników; zwraca zdarzenia ("new" | "worse" | "resolved", wiersz) do alertów."""
    now = time.time()
    existing = {_row_key(row): row for row in panel.db.fetchall("SELECT * FROM vuln_findings WHERE status != 'resolved'")}
    events: list[tuple[str, dict]] = []
    for key, found in findings.items():
        target: Target = found["target"]
        values = {"label": target.label[:190], "app_id": target.app_id, "version": target.version[:190], "severity": found["severity"],
                  "score": found["score"], "vulns": found["vulns"], "fixed": found["fixed"], "kev": int(found["kev"]), "last_seen": now}  # fmt: skip
        row = existing.pop(key, None)
        if row is None:
            row_id = panel.db.insert("vuln_findings", node_id=target.node_id, endpoint=target.endpoint, container=target.container,
                                     ecosystem=target.ecosystem, package=target.package, source=target.source, status="open",
                                     ignored_ids=[], first_seen=now, **values)  # fmt: skip
            events.append(("new", panel.db.get("vuln_findings", row_id)))
            continue
        ids = {e["id"] for e in found["vulns"]}
        state = row["status"]
        if state == "ignored" and ids - set(row.get("ignored_ids") or []):
            state = "open"  # nowa podatność zdejmuje „ignoruj”
        panel.db.update("vuln_findings", row["id"], status=state, **values)
        before = {e["id"] for e in row.get("vulns") or []}
        if state == "open" and (ids - before or row["status"] == "ignored"):
            events.append(("worse", panel.db.get("vuln_findings", row["id"])))
    nodes = {n["id"] for n in panel.db.fetchall("SELECT id FROM nodes")}
    for row in existing.values():
        lockfile = row["container"] == ""
        if row["node_id"] not in nodes or ((row["node_id"] in scanned) if lockfile else ((row["node_id"], row["endpoint"]) in covered)):
            panel.db.update("vuln_findings", row["id"], status="resolved", resolved_at=now)
            events.append(("resolved", row))
    panel.db.execute("DELETE FROM vuln_findings WHERE status = 'resolved' AND resolved_at < ?", [now - KEEP])
    return events


# --- alerty --------------------------------------------------------------------------------------


def alert_name(row: dict) -> str:
    return f"{row['label']} · {row['package']} {row['version']}"


def alert_text(row: dict) -> str:
    vulns = row.get("vulns") or []
    top = ", ".join(e["cve"] or e["id"] for e in vulns[:3])
    text = t("vuln.alert_text", count=len(vulns), severity=t(f"vuln.sev.{row['severity']}"), top=top)
    if row.get("kev"):
        text += " " + t("vuln.alert_kev")
    if row.get("fixed"):
        text += " " + t("vuln.alert_fix", version=row["fixed"])
    return text


def _alerting(panel: Any, row: dict) -> bool:
    threshold = RANK[settings(panel)["alert_severity"]]
    return bool(row.get("kev")) or RANK.get(row["severity"], 0) >= threshold


async def notify(panel: Any, events: list[tuple[str, dict]]) -> None:
    from . import alerts

    active = {r["target_id"]: r for r in panel.db.fetchall("SELECT * FROM alerts WHERE rule = 'vulnerability' AND status IN ('firing', 'ignored')")}
    for kind, row in events:
        alert = active.get(row["id"])
        if kind == "resolved":
            if alert:
                await alerts.resolve(panel, alert, t("vuln.alert_resolved", package=row["package"]))
        elif not alert and _alerting(panel, row):
            await alerts.fire(panel, "vulnerability", "vuln", row["id"], alert_name(row), row.get("score"), alert_text(row),
                              {"package": row["package"], "version": row["version"], "fixed": row.get("fixed"),
                               "cves": [e["cve"] for e in row.get("vulns") or [] if e["cve"]][:20]})  # fmt: skip


def silence(panel: Any, finding_id: int) -> None:
    """Zignorowana paczka nie trzyma alertu (bez powiadomienia „wróciło do normy”)."""
    panel.db.execute("UPDATE alerts SET status = 'resolved', resolved_at = ? WHERE rule = 'vulnerability' AND target_id = ? AND status IN ('firing', 'ignored')",
                     [time.time(), finding_id])  # fmt: skip


# --- skan ----------------------------------------------------------------------------------------


async def scan(panel: Any, job: Any = None, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Pełny skan: spis ze wszystkich sparowanych węzłów, OSV.dev, CISA KEV, zapis i alerty."""
    log = job.log if job else (lambda _line: None)
    if running(panel):
        raise LookupError(t("vuln.already_running"))
    RUNNING.add(id(panel))
    started = time.time()
    result: dict[str, Any] = {"scanned_at": started, "error": None, "nodes": {}, "packages": 0, "findings": 0}
    try:
        nodes = [n for n in panel.db.all("nodes") if n["status"] == "paired" and n.get("secret")]
        inventories = await asyncio.gather(*(_inventory(panel, node) for node in nodes))
        items: list[Target] = []
        covered: set[tuple[int, str]] = set()
        scanned: set[int] = set()
        for node, (inventory, error) in zip(nodes, inventories):
            entry = {"name": node["name"], "error": error, "containers": 0, "packages": 0}
            if inventory is not None:
                scanned.add(node["id"])
                for ep in inventory.get("endpoints") or []:
                    if ep.get("error"):
                        entry["error"] = f"{ep.get('name') or ep.get('id')}: {ep['error']}"
                    else:
                        covered.add((node["id"], ep["id"]))
                        entry["containers"] += len(ep.get("containers") or [])
                found = targets(panel, node, inventory)
                entry["packages"] = len(found)
                items += found
            log(t("vuln.log_node", node=node["name"], containers=entry["containers"], packages=entry["packages"])
                if not entry["error"] else t("vuln.log_node_error", node=node["name"], error=entry["error"]))  # fmt: skip
            result["nodes"][str(node["id"])] = entry
        keys = sorted({(i.ecosystem, i.package, i.version) for i in items})
        result["packages"] = len(keys)
        headers = {"User-Agent": f"Deployra/{__version__}"}
        async with httpx.AsyncClient(timeout=30, headers=headers, transport=transport, follow_redirects=True) as http:
            exploited = await kev(panel, http)
            log(t("vuln.log_osv", count=len(keys)))
            results = await query(http, keys)
            wanted = {ref["id"]: ref.get("modified") for refs in results.values() for ref in refs}
            records = await details(panel, http, wanted)
        findings = evaluate(items, results, records, exploited)
        events = store(panel, findings, covered, scanned)
        await suggest(panel, transport)
        result["findings"] = len(findings)
        log(t("vuln.log_done", findings=len(findings), vulns=len(wanted)))
        await notify(panel, events)
        panel.plugins.emit("vulns.scanned", findings=len(findings), new=sum(1 for kind, _ in events if kind == "new"))
    except (httpx.HTTPError, ValueError) as exc:
        result["error"] = t("vuln.osv_failed", error=str(exc)[:200] or type(exc).__name__)
        log(result["error"])
    finally:
        RUNNING.discard(id(panel))
        result["duration"] = round(time.time() - started, 1)
        panel.db.set_setting("vuln_status", result)
    if result["error"] and job:
        from deployra_common.jobs import JobFailed

        raise JobFailed(result["error"])
    return result


async def loop(panel: Any) -> None:
    """Co TICK sprawdza, czy minął interwał skanowania (domyślnie 6 h)."""
    await asyncio.sleep(FIRST_SCAN)
    while True:
        i18n_core.current.set(panel.default_language or i18n_core.DEFAULT)
        try:
            config, last = settings(panel), status(panel)
            wait = config["interval_hours"] * 3600
            if last.get("error"):
                wait = min(wait, RETRY_FAILED)
            if config["enabled"] and not running(panel) and time.time() - float(last.get("scanned_at") or 0) > wait:
                await scan(panel)
        except Exception as exc:  # noqa: BLE001 — pętla nie może paść
            logger.error(f"Skanowanie podatności: {exc}")
        await asyncio.sleep(TICK)


# --- widoki --------------------------------------------------------------------------------------


def listing(panel: Any, state: str = "open", app_id: int | None = None) -> list[dict]:
    sql, params = "SELECT * FROM vuln_findings WHERE status = ?", [state]
    if app_id is not None:
        sql, params = sql + " AND app_id = ?", [*params, app_id]
    rows = panel.db.fetchall(sql, params)
    return sorted(rows, key=lambda r: (-r["kev"], -RANK.get(r["severity"], 0), -(r["score"] or 0), r["label"], r["package"]))


def counts(rows: list[dict]) -> dict[str, int]:
    out = {name: 0 for name in SEVERITIES}
    for row in rows:
        out[row["severity"]] = out.get(row["severity"], 0) + 1
    out["kev"] = sum(1 for row in rows if row["kev"])
    return out


def banner(panel: Any) -> dict[str, Any]:
    """Licznik w menu (wysokie i gorsze) i pasek u góry panelu (krytyczne albo wykorzystywane w atakach, bez „ignoruj”)."""
    rows = panel.db.fetchall("SELECT label, severity, kev FROM vuln_findings WHERE status = 'open' AND (severity IN ('critical', 'high') OR kev = 1)")
    urgent = [r for r in rows if r["severity"] == "critical" or r["kev"]]
    show = bool(urgent) and float(panel.db.setting("vuln_banner_snooze") or 0) <= time.time()
    return {"high": len(rows), "show": show, "count": len(urgent), "targets": len({r["label"] for r in urgent}),
            "kev": sum(1 for r in urgent if r["kev"])}  # fmt: skip


# produkt z obrazu (pakiet Bitnami) → repozytoria obrazów, w których to on jest głównym oprogramowaniem
PRODUCT_REPOS = {
    "php": {"php"}, "node": {"node"}, "python": {"python"}, "nginx": {"nginx"}, "apache": {"httpd"}, "redis": {"redis"}, "valkey": {"valkey"},
    "postgresql": {"postgres"}, "mariadb": {"mariadb"}, "mysql": {"mysql"}, "mongodb": {"mongo"}, "ruby": {"ruby"}, "golang": {"golang"},
    "tomcat": {"tomcat"}, "wordpress": {"wordpress"}, "ghost": {"ghost"}, "nextcloud": {"nextcloud"}, "memcached": {"memcached"},
    "rabbitmq": {"rabbitmq"}, "haproxy": {"haproxy"}, "grafana": {"grafana"}, "gitea": {"gitea"}, "keycloak": {"keycloak"},
    "drupal": {"drupal"}, "joomla": {"joomla"}, "mediawiki": {"mediawiki"}, "redmine": {"redmine"},
}  # fmt: skip
_TAG = re.compile(r"^(v?)(\d+(?:\.\d+){0,3})((?:[-_][A-Za-z0-9.]+)*)$")


def _nums(text: str) -> tuple[int, ...]:
    match = re.match(r"^v?(\d+(?:\.\d+)*)", str(text or ""))
    return tuple(int(p) for p in match.group(1).split(".")) if match else ()


def suggest_tag(tag: str, fixed: str, tags: list[str]) -> str | None:
    """Najmniejszy skok do wersji z poprawką, w tym samym kształcie tagu: `8.0-apache` + 8.2.35 → `8.2-apache`,
    `8.0.30-apache` → `8.2.35-apache`. Bez wersji testowych (inny przyrostek) i bez zgadywania przy `latest`."""
    current, target = _TAG.match(tag), _nums(fixed)
    if not current or not target:
        return None
    prefix, nums, suffix = current.group(1), _nums(current.group(2)), current.group(3)
    depth = len(nums)
    need = target[:depth] if depth < len(target) else target
    found = []
    for candidate in tags:
        parsed = _TAG.match(candidate)
        if not parsed or parsed.group(1) != prefix or parsed.group(3) != suffix:
            continue
        version = _nums(parsed.group(2))
        if len(version) == depth and version > nums and version >= need:
            found.append((version, candidate))
    return min(found)[1] if found else None


async def suggest(panel: Any, transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Dla pozycji łatanych dopiero w nowszej gałęzi szuka w rejestrze obrazu aplikacji z poprawką (to zrobi „Napraw”)."""
    from . import app_updates
    from .deploy import load

    rows = [r for r in panel.db.fetchall("SELECT * FROM vuln_findings WHERE status = 'open'")]
    tag_cache: dict[tuple[str, str], list[str]] = {}
    headers = {"User-Agent": f"Deployra/{__version__}"}
    async with httpx.AsyncClient(timeout=20, headers=headers, transport=transport, follow_redirects=True) as http:
        registry = app_updates.Registry(panel, http)
        for row in rows:
            image = None
            app = panel.db.get("apps", row["app_id"]) if row["app_id"] else None
            if app and app["kind"] == "image" and row["ecosystem"] == "Bitnami" and new_branch(row["version"], row["fixed"]):
                try:
                    current = load(panel, app)[0].source.image or ""
                except Exception:  # noqa: BLE001
                    current = ""
                ref = app_updates.parse_ref(current)
                if ref and ref.repo.rsplit("/", 1)[-1] in PRODUCT_REPOS.get(row["package"], set()):
                    key = (ref.registry, ref.repo)
                    if key not in tag_cache:
                        try:
                            tag_cache[key] = await registry.tags(ref)
                        except (httpx.HTTPError, ValueError):
                            tag_cache[key] = []
                    tag = suggest_tag(ref.tag, row["fixed"], tag_cache[key])
                    image = app_updates.with_tag(current, tag) if tag else None
            if image != row.get("suggest"):
                panel.db.update("vuln_findings", row["id"], suggest=image)


def new_branch(version: str, fixed: str | None) -> bool:
    """Poprawka jest dopiero w innej gałęzi (8.0.30 → 8.2.35): świeży obraz tego samego tagu nic nie da, trzeba zmienić wersję."""
    return bool(fixed) and vkey(version)[0][:2] != vkey(fixed)[0][:2]


def group(rows: list[dict]) -> list[dict]:
    """Wiersze pogrupowane po kontenerze/aplikacji (kolejność: najgroźniejsze najpierw)."""
    groups: dict[tuple, dict] = {}
    for row in rows:  # aplikacja (jej kontenery i pliki lock) albo pojedynczy kontener spoza aplikacji
        key = ("app", row["app_id"]) if row["app_id"] else (row["node_id"], row["endpoint"], row["container"])
        entry = groups.setdefault(key, {"label": row["label"], "app_id": row["app_id"], "node_id": row["node_id"], "endpoint": row["endpoint"],
                                        "container": row["container"], "rows": []})  # fmt: skip
        entry["rows"].append({**row, "new_branch": new_branch(row["version"], row["fixed"])})
        if row["container"] and not entry["container"]:
            entry["container"] = row["container"]
    return list(groups.values())

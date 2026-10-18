"""Harmonogram kopii baz danych: zrzut kontenera bazy (pg_dump, mysqldump, mongodump) do magazynu kopii.

Harmonogram (5 pól cron, czas lokalny panelu) trzyma tabela `db_backup_schedules`: węzeł i NAZWA kontenera
(nie id, żeby przetrwał odtworzenie kontenera), retencja liczona osobno dla każdego harmonogramu.
Zrzut pobiera panel z węzła tak jak ręczne „pobierz zrzut” i zapisuje w magazynie pod katalogiem
`db/<węzeł>/<kontener>/s<id>_<czas>.<rodzaj>.gz`. Przywrócenie odsyła plik z powrotem do węzła.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from loguru import logger

from deployra_common import i18n as i18n_core

from ..db import now
from ..i18n import t
from . import alerts, backup_storage
from .notify import Message

TICK = 60
MAX_RETENTION = 365
CRON_ALIASES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
# minuta, godzina, dzień miesiąca, miesiąc, dzień tygodnia (0 albo 7 = niedziela)
FIELD_LIMITS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
DUMP_NAME = re.compile(r"^s(\d+)_(\d{8}-\d{6})\.(sql|archive)\.gz$")
RUNNING: dict[int, asyncio.Task] = {}  # harmonogram → trwający zrzut (jeden naraz na harmonogram)
LAST_FIRED: dict[int, int] = {}  # harmonogram → minuta (epoka // 60), w której już ruszył


class CronError(ValueError):
    pass


@dataclass(frozen=True)
class Cron:
    minute: frozenset[int]
    hour: frozenset[int]
    day: frozenset[int]
    month: frozenset[int]
    weekday: frozenset[int]
    day_any: bool
    weekday_any: bool


def _field(text: str, low: int, high: int) -> set[int]:
    """Pole cron: `*`, liczba, zakres `a-b`, lista po przecinku i krok po `/` (`*/15`, `2-10/2`, `5/10`)."""
    values: set[int] = set()
    for part in text.split(","):
        base, slashed, step_text = part.partition("/")
        if slashed and (not step_text.isdigit() or int(step_text) == 0):
            raise CronError(part)
        step = int(step_text) if slashed else 1
        if base == "*":
            start, end = low, high
        elif "-" in base:
            left, _, right = base.partition("-")
            if not (left.isdigit() and right.isdigit()):
                raise CronError(part)
            start, end = int(left), int(right)
        elif base.isdigit():
            start = int(base)
            end = high if slashed else start  # „5/10” = od 5 co 10 do końca zakresu
        else:
            raise CronError(part)
        if not low <= start <= end <= high:
            raise CronError(part)
        values.update(range(start, end + 1, step))
    return values


def parse_cron(expr: str) -> Cron:
    text = (expr or "").strip()
    text = CRON_ALIASES.get(text.lower(), text)
    parts = text.split()
    if len(parts) != 5:
        raise CronError(expr)
    try:
        minute, hour, day, month, weekday = [_field(p, low, high) for p, (low, high) in zip(parts, FIELD_LIMITS)]
    except CronError as exc:
        raise CronError(expr) from exc
    return Cron(
        minute=frozenset(minute),
        hour=frozenset(hour),
        day=frozenset(day),
        month=frozenset(month),
        weekday=frozenset(0 if d == 7 else d for d in weekday),
        day_any=parts[2].startswith("*"),
        weekday_any=parts[4].startswith("*"),
    )


def cron_matches(expr: str | Cron, when: datetime) -> bool:
    """Czy harmonogram uruchamia się w minucie `when`. Dzień miesiąca i tygodnia: jeśli oba są ograniczone, wystarczy jeden."""
    cron = expr if isinstance(expr, Cron) else parse_cron(expr)
    if when.minute not in cron.minute or when.hour not in cron.hour or when.month not in cron.month:
        return False
    day_ok = when.day in cron.day
    weekday_ok = (when.weekday() + 1) % 7 in cron.weekday  # Python: pon = 0; cron: niedziela = 0
    if cron.day_any and cron.weekday_any:
        return True
    if cron.day_any:
        return weekday_ok
    if cron.weekday_any:
        return day_ok
    return day_ok or weekday_ok


def folder_for(node_name: str, container: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", node_name or "").strip("-") or "node"
    return f"db/{safe}/{container}"


def dump_file_name(sid: int, stamp: str, action: str) -> str:
    kind = "archive" if action == "dump_archive" else "sql"
    return f"s{sid}_{stamp}.{kind}.gz"


def dumps_of(files: list[dict[str, Any]], sid: int) -> list[dict[str, Any]]:
    """Kopie jednego harmonogramu z katalogu magazynu, najnowsze pierwsze."""
    out = []
    for item in files:
        match = DUMP_NAME.match(item["name"])
        if match and int(match.group(1)) == sid:
            out.append({"name": item["name"], "size": item["size"], "stamp": match.group(2)})
    return sorted(out, key=lambda d: d["stamp"], reverse=True)


def apply_retention(driver: Any, folder: str, sid: int, keep: int) -> int:
    """Usuwa kopie ponad limit `keep` (od najstarszej). Zwraca liczbę usuniętych plików."""
    old = dumps_of(driver.list_files(folder), sid)[keep:]
    removed = 0
    for item in old:
        try:
            driver.delete(f"{folder}/{item['name']}")
            removed += 1
        except Exception as exc:  # noqa: BLE001 — nieusunięty stary plik nie psuje świeżej kopii
            logger.warning(f"Kopie baz: nie usunięto {item['name']}: {exc}")
    return removed


# --- harmonogramy (CRUD) --------------------------------------------------------------------------


def _check(cron: str, retention: Any) -> tuple[str, int]:
    try:
        parse_cron(cron)
    except CronError as exc:
        raise ValueError(t("backup.db_err_cron", value=cron)) from exc
    try:
        keep = int(retention)
    except (TypeError, ValueError) as exc:
        raise ValueError(t("backup.db_err_retention")) from exc
    if not 1 <= keep <= MAX_RETENTION:
        raise ValueError(t("backup.db_err_retention"))
    return cron.strip(), keep


def schedules(panel: Any) -> list[dict[str, Any]]:
    return panel.db.fetchall(
        "SELECT s.*, n.name AS node_name, n.status AS node_status FROM db_backup_schedules s "
        "LEFT JOIN nodes n ON n.id = s.node_id ORDER BY s.id"
    )


def get(panel: Any, sid: int) -> dict[str, Any]:
    row = panel.db.fetchone(
        "SELECT s.*, n.name AS node_name, n.status AS node_status FROM db_backup_schedules s "
        "LEFT JOIN nodes n ON n.id = s.node_id WHERE s.id = ?",
        [sid],
    )
    if not row:
        raise ValueError(t("backup.db_err_missing"))
    return row


def create(panel: Any, node_id: int, container: str, cron: str, retention: Any, enabled: bool = True) -> int:
    container = (container or "").strip()
    if not CONTAINER_NAME.match(container):
        raise ValueError(t("backup.db_err_container"))
    node = panel.db.fetchone("SELECT id, name FROM nodes WHERE id = ?", [node_id])
    if not node:
        raise ValueError(t("backup.db_err_node"))
    cron, keep = _check(cron, retention)
    return panel.db.insert(
        "db_backup_schedules",
        node_id=node["id"],
        container=container,
        folder=folder_for(node["name"], container),
        cron=cron,
        retention=keep,
        enabled=1 if enabled else 0,
        created_at=now(),
    )


def update(panel: Any, sid: int, cron: str, retention: Any, enabled: bool) -> None:
    get(panel, sid)
    cron, keep = _check(cron, retention)
    panel.db.update("db_backup_schedules", sid, cron=cron, retention=keep, enabled=1 if enabled else 0)


def delete(panel: Any, sid: int) -> None:
    """Usuwa harmonogram; zapisane kopie zostają w magazynie."""
    get(panel, sid)
    panel.db.execute("DELETE FROM db_backup_schedules WHERE id = ?", [sid])


def valid_dump_name(sid: int, name: str) -> bool:
    match = DUMP_NAME.match(name or "")
    return bool(match) and int(match.group(1)) == sid


def stored_dumps(panel: Any, sid: int) -> list[dict[str, Any]]:
    sched = get(panel, sid)
    driver = backup_storage.get_driver(panel)
    return dumps_of(driver.list_files(sched["folder"]), sid)


def read_dump(panel: Any, sid: int, name: str) -> bytes:
    if not valid_dump_name(sid, name):
        raise ValueError(t("backup.db_err_file"))
    sched = get(panel, sid)
    return backup_storage.get_driver(panel).get(f"{sched['folder']}/{name}")


# --- zrzut i przywracanie -------------------------------------------------------------------------


def error_text(exc: BaseException) -> str:
    detail = getattr(exc, "detail", None)
    return str(detail or exc or type(exc).__name__)[:500]


def _record(panel: Any, sid: int, **fields: Any) -> None:
    panel.db.update("db_backup_schedules", sid, **fields)


async def _profile(client: Any, name: str) -> dict[str, Any]:
    return await client.get(f"/api/containers/{name}/profile")


async def run(panel: Any, sid: int) -> bool:
    """Wykonuje zrzut harmonogramu i zapisuje wynik; błąd trafia do powiadomień i alertów (nie przerywa harmonogramu)."""
    name = str(sid)
    try:
        sched = get(panel, sid)
        name = sched["container"]
        node = panel.db.fetchone("SELECT * FROM nodes WHERE id = ?", [sched["node_id"]])
        if not node:
            raise ValueError(t("backup.db_err_node"))
        client = panel.node_client(node)
        profile = await _profile(client, name)
        if profile.get("category") != "database":
            raise ValueError(t("backup.db_err_not_database"))
        action = "dump_archive" if profile.get("kind") == "mongodb" else "dump_sql"
        res = await client.request("GET", f"/api/containers/{name}/action/quick/{action}?ep=local", timeout=600)
        content = res.content
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        file_name = dump_file_name(sid, stamp, action)
        driver = backup_storage.get_driver(panel)
        await asyncio.to_thread(driver.put, f"{sched['folder']}/{file_name}", content)
        try:  # zapisany zrzut liczy się jako udany, nawet gdy sprzątanie starych się nie powiedzie
            await asyncio.to_thread(apply_retention, driver, sched["folder"], sid, sched["retention"])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Kopia bazy {name}: retencja nieudana: {error_text(exc)}")
        _record(panel, sid, last_run=time.time(), last_status="ok", last_message="", last_filename=file_name, last_size=len(content))
        return True
    except Exception as exc:  # noqa: BLE001 — błąd jednego zrzutu nie zatrzymuje harmonogramu
        message = error_text(exc)
        logger.warning(f"Kopia bazy {name}: {message}")
        try:
            _record(panel, sid, last_run=time.time(), last_status="error", last_message=message)
        except Exception:  # noqa: BLE001 — harmonogram mógł zostać usunięty w trakcie
            pass
        panel.notify(t("backup.db_failed", name=name, error=message), "err")
        await alerts.event(panel, "db_backup_failed", Message(t("alerts.title.db_backup_failed", name=name), message, "err"))
        return False
    finally:
        if RUNNING.get(sid) is asyncio.current_task():
            RUNNING.pop(sid, None)


def is_running(sid: int) -> bool:
    task = RUNNING.get(sid)
    return task is not None and not task.done() and not task.get_loop().is_closed()


def start(panel: Any, sid: int) -> bool:
    """Startuje zrzut w tle. False, gdy poprzedni zrzut tego harmonogramu jeszcze trwa."""
    get(panel, sid)
    if is_running(sid):
        return False
    RUNNING[sid] = asyncio.create_task(run(panel, sid))
    return True


async def restore(panel: Any, sid: int, name: str) -> str:
    """Przywraca zapisaną kopię do kontenera (backend czyta plik, czyści schemat i importuje). Zwraca nazwę kontenera."""
    if not valid_dump_name(sid, name):
        raise ValueError(t("backup.db_err_file"))
    sched = get(panel, sid)
    node = panel.db.fetchone("SELECT * FROM nodes WHERE id = ?", [sched["node_id"]])
    if not node:
        raise ValueError(t("backup.db_err_node"))
    data = await asyncio.to_thread(backup_storage.get_driver(panel).get, f"{sched['folder']}/{name}")
    client = panel.node_client(node)
    res = await client.request(
        "POST",
        f"/api/containers/{sched['container']}/action/restore?ep=local",
        content=data,
        headers={"Content-Type": "application/gzip"},
        timeout=900,
    )
    body = res.json() if res.content else {}
    if body.get("exit_code", 0) != 0:
        raise ValueError(t("backup.db_restore_failed", code=body.get("exit_code"), output=(body.get("output") or "")[-300:]))
    return sched["container"]


async def tick(panel: Any, when: datetime | None = None) -> list[int]:
    """Uruchamia harmonogramy pasujące do minuty `when`. Zwraca id ruszonych zrzutów."""
    when = (when or datetime.now()).replace(second=0, microsecond=0)
    minute = int(when.timestamp() // 60)
    started = []
    for sched in panel.db.fetchall("SELECT * FROM db_backup_schedules WHERE enabled = 1"):
        sid = sched["id"]
        try:
            due = cron_matches(sched["cron"], when)
        except CronError:
            logger.warning(f"Kopia bazy {sid}: nieprawidłowy harmonogram {sched['cron']!r}")
            continue
        if not due or LAST_FIRED.get(sid) == minute:
            continue
        LAST_FIRED[sid] = minute
        if start(panel, sid):
            started.append(sid)
        else:
            logger.info(f"Kopia bazy {sid}: poprzedni zrzut jeszcze trwa, pomijam")
    return started


async def loop(panel: Any) -> None:
    i18n_core.current.set(panel.default_language or i18n_core.DEFAULT)  # komunikaty z tła — w języku domyślnym panelu
    await asyncio.sleep(15)
    while True:
        try:
            await tick(panel)
        except Exception as exc:  # noqa: BLE001 — pętla harmonogramu nie może paść
            logger.error(f"Kopie baz: {type(exc).__name__}: {exc}")
        await asyncio.sleep(TICK - (time.time() % TICK) + 0.5)  # kolejne sprawdzenie na początku minuty


# --- wybór kontenerów ------------------------------------------------------------------------------


async def _profile_or_none(client: Any, name: str) -> dict[str, Any] | None:
    try:
        return await asyncio.wait_for(_profile(client, name), timeout=10)
    except Exception:  # noqa: BLE001 — kontener bez profilu nie wchodzi na listę
        return None


async def _node_databases(panel: Any, node: dict[str, Any]) -> list[dict[str, Any]]:
    client = panel.node_client(node)
    try:
        containers = await asyncio.wait_for(client.get("/api/containers"), timeout=10)
    except Exception:  # noqa: BLE001 — węzeł offline nie blokuje listy
        return []
    running = [c for c in containers or [] if c.get("state") == "running"]
    profiles = await asyncio.gather(*(_profile_or_none(client, c["name"]) for c in running))
    return [
        {"node_id": node["id"], "node": node["name"], "container": c["name"], "title": p.get("title", ""), "image": c.get("image") or ""}
        for c, p in zip(running, profiles)
        if p and p.get("category") == "database"
    ]


async def database_containers(panel: Any) -> list[dict[str, Any]]:
    """Uruchomione kontenery baz danych (kategoria „database” z profilu) na wszystkich sparowanych węzłach."""
    nodes = panel.db.fetchall("SELECT * FROM nodes WHERE status = 'paired' ORDER BY name")
    groups = await asyncio.gather(*(_node_databases(panel, node) for node in nodes))
    return [item for group in groups for item in group]

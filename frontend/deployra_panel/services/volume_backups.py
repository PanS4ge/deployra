"""Kopie wolumenów aplikacji: harmonogram (cron), retencja, przywracanie — do magazynu kopii panelu (S3/R2, SFTP, FTP, dysk).

Węzeł pakuje dane aplikacji tak jak przy przenoszeniu (`/api/apps/<slug>/export`: wolumeny, pliki strony, stack compose),
panel pobiera paczkę, szyfruje ją i wysyła do magazynu. Przywracanie: pobranie, odszyfrowanie, wgranie paczki na węzeł,
zatrzymanie aplikacji, `/import`, start.

Szyfrowanie strumieniowe AES-256-GCM w kawałkach po 1 MiB, klucz wyprowadzony z `secret.key` panelu. Każdy kawałek ma
własny nonce i w danych uwierzytelnianych numer oraz znacznik „ostatni” — podmiana kolejności, usunięcie kawałka albo
obcięcie pliku wychodzą przy odszyfrowaniu. Bez `secret.key` (np. po utracie panelu) kopii nie da się odczytać —
dlatego `secret.key` trzeba mieć w osobnej, bezpiecznej kopii.
"""

from __future__ import annotations

import os
import re
import secrets
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from loguru import logger

from deployra_common.client import RemoteError
from deployra_common.jobs import Job, JobFailed

from ..db import now
from ..i18n import t
from . import relay
from .cron import matches, validate

MAGIC = b"DPLVB1\n"
CHUNK = 1 << 20
MAX_BYTES = 2 << 30  # magazyny przyjmują całość naraz — powyżej 2 GiB lepiej kopia po stronie węzła/dysku
NAME = re.compile(r"^v(\d+)_(\d{8}-\d{6})\.dvb$")
LAST_FIRED: dict[int, int] = {}


class BackupError(Exception):
    pass


# --- szyfrowanie ------------------------------------------------------------------------------------


def _key(panel: Any) -> bytes:
    return panel.sealer.derive("volume-backup")


def _aad(index: int, last: bool) -> bytes:
    return MAGIC + index.to_bytes(4, "big") + (b"\x01" if last else b"\x00")


def encrypt_file(panel: Any, source: Path, target: Path) -> int:
    aes, base = AESGCM(_key(panel)), secrets.token_bytes(8)
    written = 0
    with open(source, "rb") as src, open(target, "wb") as out:
        out.write(MAGIC + base)
        index, chunk = 0, src.read(CHUNK)
        while True:
            nxt = src.read(CHUNK)
            last = not nxt
            sealed = aes.encrypt(base + index.to_bytes(4, "big"), chunk, _aad(index, last))
            out.write(len(sealed).to_bytes(4, "big") + (b"\x01" if last else b"\x00") + sealed)
            written += len(chunk)
            if last:
                break
            index, chunk = index + 1, nxt
    return written


def decrypt_to(panel: Any, data: bytes, target: Path) -> int:
    if not data.startswith(MAGIC) or len(data) < len(MAGIC) + 8:
        raise BackupError(t("vbackup.bad_file"))
    aes, base = AESGCM(_key(panel)), data[len(MAGIC):len(MAGIC) + 8]
    pos, index, size, finished = len(MAGIC) + 8, 0, 0, False
    with open(target, "wb") as out:
        while pos < len(data):
            if finished or pos + 5 > len(data):
                raise BackupError(t("vbackup.bad_file"))
            length, last = int.from_bytes(data[pos:pos + 4], "big"), data[pos + 4] == 1
            sealed = data[pos + 5:pos + 5 + length]
            if len(sealed) != length:
                raise BackupError(t("vbackup.truncated"))
            try:
                plain = aes.decrypt(base + index.to_bytes(4, "big"), sealed, _aad(index, last))
            except Exception as exc:  # noqa: BLE001 — InvalidTag: inny klucz albo zmienione dane
                raise BackupError(t("vbackup.bad_key")) from exc
            out.write(plain)
            size += len(plain)
            pos, index, finished = pos + 5 + length, index + 1, last
    if not finished:
        raise BackupError(t("vbackup.truncated"))
    return size


# --- harmonogramy -----------------------------------------------------------------------------------


def schedule_for(panel: Any, app_id: int) -> dict | None:
    return panel.db.fetchone("SELECT * FROM volume_backups WHERE app_id = ?", [app_id])


def save(panel: Any, app_id: int, cron: str, retention: Any, stop_app: bool, enabled: bool) -> int:
    cron = cron.strip()
    try:
        validate(cron)
    except ValueError as exc:
        raise ValueError(t("vbackup.bad_cron", error=exc)) from None
    keep = max(1, min(int(retention or 7), 365))
    existing = schedule_for(panel, app_id)
    values = {"cron": cron, "retention": keep, "stop_app": int(stop_app), "enabled": int(enabled)}
    if existing:
        panel.db.update("volume_backups", existing["id"], **values)
        return existing["id"]
    return panel.db.insert("volume_backups", app_id=app_id, created_at=now(), **values)


def folder(app: dict) -> str:
    return f"volumes/{re.sub(r'[^A-Za-z0-9._-]+', '-', app['slug'])}"


def stored(panel: Any, sched: dict) -> list[dict]:
    from .backup_storage import get_driver

    app = panel.db.get("apps", sched["app_id"])
    if not app:
        return []
    out = []
    for item in get_driver(panel).list_files(folder(app)):
        match = NAME.match(item["name"])
        if match and int(match.group(1)) == sched["id"]:
            out.append({"name": item["name"], "size": item["size"], "stamp": match.group(2)})
    return sorted(out, key=lambda d: d["stamp"], reverse=True)


# --- kopia i przywracanie --------------------------------------------------------------------------


async def run(panel: Any, job: Job, sid: int) -> dict:
    from .backup_storage import get_driver
    from .migrate import _human

    sched = panel.db.get("volume_backups", sid)
    app = panel.db.get("apps", (sched or {}).get("app_id") or 0)
    node = panel.node(app["node_id"]) if app else None
    if not sched or not app or not node:
        raise JobFailed(t("vbackup.no_app"))
    from . import deploy

    spec, _ = deploy.load(panel, app)
    client = panel.node_client(node)
    stop = bool(sched["stop_app"]) and app["status"] == "running"
    job.log(t("vbackup.start", app=app["name"], node=node["name"]))
    panel.db.update("volume_backups", sid, last_status="running", last_error=None)
    tmp_dir = panel.config.data_dir / "uploads"
    plain = tempfile.NamedTemporaryFile(dir=tmp_dir, suffix=".tar", delete=False).name
    sealed = plain + ".dvb"
    transfer = None
    try:
        exported = await relay.start_and_follow(client, f"/api/apps/{spec.slug}/export", {"spec": spec.model_dump(mode="json"), "stop": stop}, job, "  ")
        transfer = exported["result"]["transfer"]
        size = 0
        with open(plain, "wb") as out:
            async with client.stream("GET", f"/api/transfers/{transfer}", timeout=httpx.Timeout(30.0, read=600.0)) as response:
                async for chunk in response.aiter_bytes(CHUNK):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise JobFailed(t("vbackup.too_big", size=_human(MAX_BYTES)))
                    out.write(chunk)
        job.log(t("vbackup.downloaded", size=_human(size)))
        encrypt_file(panel, Path(plain), Path(sealed))
        name = f"v{sid}_{datetime.now().strftime('%Y%m%d-%H%M%S')}.dvb"
        driver = get_driver(panel)
        await _thread(driver.put, f"{folder(app)}/{name}", Path(sealed).read_bytes())
        job.log(t("vbackup.uploaded", name=name))
        removed = 0
        for item in stored(panel, sched)[int(sched["retention"]):]:
            try:
                await _thread(driver.delete, f"{folder(app)}/{item['name']}")
                removed += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Kopia wolumenów: nie usunięto {item['name']}: {exc}")
        if removed:
            job.log(t("vbackup.pruned", count=removed))
        panel.db.update("volume_backups", sid, last_status="ok", last_run_at=time.time(), last_size=size, last_error=None)
        return {"name": name, "size": size}
    except (JobFailed, RemoteError, httpx.HTTPError, OSError, BackupError, ValueError) as exc:
        message = str(getattr(exc, "detail", None) or exc) or type(exc).__name__
        panel.db.update("volume_backups", sid, last_status="error", last_run_at=time.time(), last_error=message[:500])
        from .notify import Message
        from . import alerts

        await alerts.event(panel, "volume_backup_failed", Message(t("alerts.title.volume_backup_failed", name=app["name"]), message[:1500], "err"))
        raise JobFailed(message) from exc
    finally:
        for path in (plain, sealed):
            try:
                os.unlink(path)
            except OSError:
                pass
        if transfer:
            try:
                await client.delete(f"/api/transfers/{transfer}")
            except (RemoteError, httpx.HTTPError):
                pass
        if stop:  # eksport zatrzymał aplikację — wraca niezależnie od wyniku
            try:
                await relay.start_and_follow(client, f"/api/apps/{spec.slug}/power", {"kind": spec.source.kind, "running": True}, job, "  ")
            except (JobFailed, RemoteError, httpx.HTTPError) as exc:
                job.log(t("vbackup.restart_failed", error=getattr(exc, "detail", None) or exc))


async def restore(panel: Any, job: Job, sid: int, name: str) -> dict:
    from .backup_storage import get_driver

    sched = panel.db.get("volume_backups", sid)
    app = panel.db.get("apps", (sched or {}).get("app_id") or 0)
    node = panel.node(app["node_id"]) if app else None
    match = NAME.match(name or "")
    if not sched or not app or not node or not match or int(match.group(1)) != sid:
        raise JobFailed(t("vbackup.no_backup"))
    from . import deploy

    spec, _ = deploy.load(panel, app)
    client = panel.node_client(node)
    job.log(t("vbackup.restoring", name=name, app=app["name"]))
    data = await _thread(get_driver(panel).get, f"{folder(app)}/{name}")
    plain = tempfile.NamedTemporaryFile(dir=panel.config.data_dir / "uploads", suffix=".tar", delete=False).name
    try:
        try:
            decrypt_to(panel, data, Path(plain))
        except BackupError as exc:
            raise JobFailed(str(exc)) from exc
        uploaded = await client.upload_file("/api/transfers", plain, timeout=6 * 3600)
        await relay.start_and_follow(client, f"/api/apps/{spec.slug}/power", {"kind": spec.source.kind, "running": False}, job, "  ")
        try:
            await relay.start_and_follow(client, f"/api/apps/{spec.slug}/import", {"transfer": uploaded["transfer"]}, job, "  ")
        finally:
            await relay.start_and_follow(client, f"/api/apps/{spec.slug}/power", {"kind": spec.source.kind, "running": True}, job, "  ")
        job.log(t("vbackup.restored"))
        return {"restored": name}
    finally:
        try:
            os.unlink(plain)
        except OSError:
            pass


async def _thread(fn, *args):
    import asyncio

    return await asyncio.to_thread(fn, *args)


def start(panel: Any, sid: int) -> Any:
    sched = panel.db.get("volume_backups", sid)
    app = panel.db.get("apps", sched["app_id"]) if sched else None
    return panel.jobs.start("volume_backup", t("vbackup.job", name=(app or {}).get("name", "?")), lambda job: run(panel, job, sid))


async def tick(panel: Any, when: datetime | None = None) -> list[int]:
    when = (when or datetime.now()).replace(second=0, microsecond=0)
    minute = int(when.timestamp() // 60)
    started = []
    for sched in panel.db.fetchall("SELECT * FROM volume_backups WHERE enabled = 1"):
        try:
            due = matches(sched["cron"], when)
        except ValueError:
            continue
        if not due or LAST_FIRED.get(sched["id"]) == minute or sched.get("last_status") == "running":
            continue
        LAST_FIRED[sched["id"]] = minute
        start(panel, sched["id"])
        started.append(sched["id"])
    return started

"""Import z Nginx Proxy Managera: wgranie bazy / logowanie do API → podgląd i mapowanie → import."""

from __future__ import annotations

import os
import secrets
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile

from ..context import Panel, get_panel
from ..deps import current_user, require
from ..permissions import Permission
from ..services import npm_import
from ..web import redirect, render, toast

router = APIRouter(dependencies=[Depends(current_user), Depends(require(Permission.ALLOW_IMPORT))])
MAX_DB = 200 * 1024 * 1024


def _stash_path(panel: Panel, token: str) -> Path:
    if not token.isalnum():
        raise HTTPException(400, "zły identyfikator importu")
    return panel.config.data_dir / "uploads" / f"npm-{token}.sealed"


async def _save_upload(upload: UploadFile, directory: Path, suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix, dir=directory)
    size = 0
    with os.fdopen(fd, "wb") as out:
        while chunk := await upload.read(1 << 20):
            size += len(chunk)
            if size > MAX_DB:
                raise ValueError("plik jest za duży")
            out.write(chunk)
    return path


@router.get("/import/npm")
def import_page(request: Request):
    return render(request, "import/npm.html", title="Import z Nginx Proxy Managera")


@router.post("/import/npm/analyze")
async def analyze(request: Request, panel: Panel = Depends(get_panel)):
    form = await request.form()
    uploads = panel.config.data_dir / "uploads"
    temp_files = []
    try:
        mode = str(form.get("mode") or "file")
        if mode == "api":
            data = await npm_import.read_api(str(form.get("url") or ""), str(form.get("email") or ""),
                                             str(form.get("password") or ""), str(form.get("code") or ""))  # fmt: skip
        else:
            db_file = form.get("database")
            if not isinstance(db_file, UploadFile) or not db_file.filename:
                return toast("Wybierz plik database.sqlite", "err", status=400)
            db_path = await _save_upload(db_file, uploads, ".sqlite")
            temp_files.append(db_path)
            data = npm_import.read_sqlite(db_path)
        archive = form.get("archive")
        if isinstance(archive, UploadFile) and archive.filename:
            archive_path = await _save_upload(archive, uploads, ".tar")
            temp_files.append(archive_path)
            npm_import.read_archive(data, archive_path)
    except (ValueError, OSError) as exc:
        return toast(f"Nie udało się wczytać danych NPM: {exc}", "err", status=400)
    except Exception as exc:  # noqa: BLE001 — np. sqlite3.DatabaseError, tarfile.ReadError, httpx
        return toast(f"Nie udało się wczytać danych NPM: {type(exc).__name__}: {exc}", "err", status=400)
    finally:
        for path in temp_files:
            os.unlink(path)
    token = secrets.token_hex(8)
    _stash_path(panel, token).write_text(panel.sealer.seal(data.to_json()), encoding="utf-8")
    return redirect(request, f"/import/npm/{token}")


@router.get("/import/npm/{token}")
def preview(request: Request, token: str, panel: Panel = Depends(get_panel)):
    path = _stash_path(panel, token)
    if not path.exists():
        return redirect(request, "/import/npm", "Sesja importu wygasła — wczytaj dane ponownie", "warn")
    data = npm_import.NpmData.from_json(panel.sealer.unseal(path.read_text(encoding="utf-8")))
    existing = {d for s in panel.db.all("sites") for d in s["domains"]}
    nodes = panel.db.fetchall("SELECT * FROM nodes WHERE status = 'paired' ORDER BY name")
    return render(request, "import/preview.html", title="Import z NPM — podgląd", token=token, data=data,
                  hosts=npm_import.forward_hosts(data), existing=existing, nodes=nodes, pems=len(data.pems))  # fmt: skip


@router.post("/import/npm/{token}/apply")
async def apply(request: Request, token: str, panel: Panel = Depends(get_panel)):
    path = _stash_path(panel, token)
    if not path.exists():
        return toast("Sesja importu wygasła", "err", status=410)
    data = npm_import.NpmData.from_json(panel.sealer.unseal(path.read_text(encoding="utf-8")))
    form = await request.form()
    mapping = {}
    for index, entry in enumerate(npm_import.forward_hosts(data)):
        choice = str(form.get(f"map_{index}") or "address")
        if choice.startswith("node:"):
            mapping[entry["host"]] = {"type": "node", "node_id": int(choice[5:])}
        else:
            via = str(form.get(f"via_{index}") or "")
            mapping[entry["host"]] = {"type": "address", "via_node": int(via) if via.isdigit() else None}
    options = {
        "proxy": bool(form.get("proxy")), "redirects": bool(form.get("redirects")), "dead": bool(form.get("dead")),
        "streams": bool(form.get("streams")), "acls": bool(form.get("acls")), "certs": str(form.get("certs") or "reissue"),
    }  # fmt: skip
    job = panel.jobs.start("import", "Import z Nginx Proxy Managera", lambda job: npm_import.run(panel, job, data, mapping, options))
    path.unlink(missing_ok=True)
    return redirect(request, f"/jobs/{job.id}")


@router.post("/import/npm/{token}/cancel")
def cancel(request: Request, token: str, panel: Panel = Depends(get_panel)):
    _stash_path(panel, token).unlink(missing_ok=True)
    return redirect(request, "/import/npm", "Anulowano import")

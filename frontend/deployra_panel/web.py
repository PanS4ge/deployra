"""Szablony, filtry i pomocnicze odpowiedzi (HTMX, toasty, przekierowania)."""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from . import NAME, __version__
from .permissions import LABELS, Permission, has

BASE = Path(__file__).parent
_EP = re.compile(r"^(local|ct-\d{1,9})$")
templates = Jinja2Templates(directory=str(BASE / "templates"))


def human_bytes(value: Any) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _to_ts(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def ago(value: Any) -> str:
    ts = _to_ts(value)
    if ts is None:
        return "—"
    delta = int(time.time() - ts)
    future = delta < 0
    delta = abs(delta)
    for size, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if delta >= size:
            text = f"{delta // size} {unit}"
            break
    else:
        text = f"{delta} s"
    return f"za {text}" if future else f"{text} temu"


def duration(seconds: Any) -> str:
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "—"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {minutes} min"
    return f"{minutes} min"


def dt(value: Any) -> str:
    ts = _to_ts(value)
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M") if ts else "—"


def gravatar(email: str, size: int = 64, default: str = "identicon") -> str:
    digest = hashlib.sha256(email.strip().lower().encode()).hexdigest()
    return f"https://www.gravatar.com/avatar/{digest}?s={size}&d={default}"


def pct(value: Any) -> str:
    if value is None:
        return "—"
    value = float(value)
    return f"{value:.0f}%" if value >= 100 else f"{value:.2f}%"


def tojson_attr(value: Any) -> Markup:
    return Markup(json.dumps(value, ensure_ascii=False).replace("&", "&amp;").replace("'", "&#39;").replace('"', "&quot;"))


templates.env.filters.update(pct=pct, bytes=human_bytes, ago=ago, dt=dt, duration=duration, jsonattr=tojson_attr)
def update_offer(panel: Any) -> dict | None:
    from .services import updates  # leniwie: serwis importuje httpx

    return updates.offer(panel)


templates.env.globals.update(
    NAME=NAME, VERSION=__version__, P=Permission, PERM_LABELS=LABELS, has=has, gravatar=gravatar, now=time.time,
    update_offer=update_offer,
)


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def render(request: Request, template_name: str, status_code: int = 200, headers: dict | None = None, **context: Any) -> HTMLResponse:
    user = getattr(request.state, "user", None)
    ep = request.query_params.get("ep", "local")
    if not _EP.match(ep):
        ep = "local"
    base = {
        "request": request,
        "user": user,
        "csrf": getattr(request.state, "csrf", ""),
        "path": request.url.path,
        "panel": request.app.state.panel,
        "ep": ep,
        "epq": "" if ep == "local" else f"?ep={ep}",
    }
    return templates.TemplateResponse(request, template_name, {**base, **context}, status_code=status_code, headers=headers)


def toast_headers(message: str, level: str = "ok", extra: dict | None = None) -> dict[str, str]:
    events = {"toast": {"message": message, "level": level}, **(extra or {})}
    return {"HX-Trigger": json.dumps(events)}  # nagłówki HTTP są latin-1, więc polskie znaki idą jako escape JSON


def toast(message: str, level: str = "ok", status: int = 200, extra: dict | None = None) -> Response:
    """Pusta odpowiedź z samym powiadomieniem (dla akcji HTMX)."""
    return Response(status_code=status, headers=toast_headers(message, level, extra))


def redirect(request: Request, url: str, message: str | None = None, level: str = "ok") -> Response:
    if is_htmx(request):
        response = Response(status_code=204, headers={"HX-Redirect": url})
    else:
        response = RedirectResponse(url, status_code=303)
    if message:  # komunikat pokaże się po przeładowaniu strony (ciasteczko dpl_flash)
        flash(response, message, level)
    return response


def flash(response: Response, message: str, level: str = "ok") -> None:
    """Komunikat do pokazania po przeładowaniu (URL-encoded, bo cookie nie lubi polskich znaków)."""
    value = quote(json.dumps({"m": message, "l": level}, ensure_ascii=False), safe="")
    response.set_cookie("dpl_flash", value, max_age=30, samesite="lax", path="/")


def refresh(message: str | None = None, level: str = "ok") -> Response:
    headers = {"HX-Refresh": "true"}
    response = Response(status_code=204, headers=headers)
    if message:
        flash(response, message, level)
    return response

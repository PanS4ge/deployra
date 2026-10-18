"""Zmienne współdzielone: nazwane zestawy zmiennych (zaszyfrowane jak sekrety aplikacji) dołączane do wielu aplikacji.

Przy wdrożeniu efektywne zmienne to zestawy w kolejności dołączenia, a na końcu zmienne samej aplikacji —
te nadpisują zestawy. Zmiana zestawu nie wdraża aplikacji sama; wchodzi przy następnym wdrożeniu.
"""

from __future__ import annotations

import json
from typing import Any

from deployra_common.envfile import Layer, merge
from deployra_common.schemas import AppSpec

from ..context import Panel
from ..db import now
from ..i18n import t

NAME_MAX = 60


def _unseal(panel: Panel, payload: str) -> Layer:
    blob = json.loads(panel.sealer.unseal(payload))
    return dict(blob.get("env") or {}), dict(blob.get("scopes") or {})


def _seal(panel: Panel, env: dict[str, str], scopes: dict[str, str]) -> str:
    return panel.sealer.seal(json.dumps({"env": env, "scopes": scopes}, ensure_ascii=False))


def _name(panel: Panel, name: str, group_id: int | None) -> str:
    name = name.strip()
    if not name or len(name) > NAME_MAX:
        raise ValueError(t("envg.msg.name_required"))
    clash = panel.db.fetchone("SELECT id FROM env_groups WHERE name = ?", [name])
    if clash and clash["id"] != group_id:
        raise ValueError(t("envg.msg.name_taken", name=name))
    return name


def get(panel: Panel, group_id: int) -> dict[str, Any] | None:
    row = panel.db.get("env_groups", group_id)
    if not row:
        return None
    env, scopes = _unseal(panel, row["payload"])
    return {"id": row["id"], "name": row["name"], "env": env, "scopes": scopes}


def save(panel: Panel, group_id: int | None, name: str, env: dict[str, str], scopes: dict[str, str]) -> int:
    """Tworzy zestaw albo zmienia nazwę i zmienne istniejącego (zakres „runtime” nie jest zapisywany)."""
    if group_id is not None and not panel.db.get("env_groups", group_id):
        raise ValueError(t("envg.msg.missing"))
    name = _name(panel, name, group_id)
    scopes = {k: v for k, v in scopes.items() if v != "runtime" and k in env}
    if group_id is None:
        return panel.db.insert("env_groups", name=name, payload=_seal(panel, env, scopes), created_at=now())
    panel.db.update("env_groups", group_id, name=name, payload=_seal(panel, env, scopes))
    return group_id


def delete(panel: Panel, group_id: int) -> None:
    panel.db.execute("DELETE FROM app_env_groups WHERE group_id = ?", [group_id])
    panel.db.delete("env_groups", group_id)


def list_groups(panel: Panel) -> list[dict[str, Any]]:
    """Zestawy bez wartości: nazwa, liczba zmiennych i nazwy aplikacji, które je używają."""
    users: dict[int, list[str]] = {}
    for row in panel.db.fetchall(
        "SELECT ag.group_id, a.name FROM app_env_groups ag JOIN apps a ON a.id = ag.app_id ORDER BY a.name"
    ):  # fmt: skip
        users.setdefault(row["group_id"], []).append(row["name"])
    out = []
    for row in panel.db.fetchall("SELECT id, name, payload FROM env_groups ORDER BY name"):
        env, _ = _unseal(panel, row["payload"])
        out.append({"id": row["id"], "name": row["name"], "count": len(env), "apps": users.get(row["id"], [])})
    return out


def attached_ids(panel: Panel, app_id: int) -> list[int]:
    """Zestawy dołączone do aplikacji w kolejności (od pierwszego, który jest nadpisywany przez kolejne)."""
    rows = panel.db.fetchall("SELECT group_id FROM app_env_groups WHERE app_id = ? ORDER BY position, group_id", [app_id])
    return [row["group_id"] for row in rows]


def resolve_ids(panel: Panel, group_ids: list[int]) -> list[int]:
    """Sprawdza, że wszystkie zestawy istnieją, i usuwa powtórzenia (pierwsze wystąpienie decyduje o kolejności)."""
    unique = list(dict.fromkeys(group_ids))
    for group_id in unique:
        if not panel.db.get("env_groups", group_id):
            raise ValueError(t("envg.msg.missing"))
    return unique


def set_attached(panel: Panel, app_id: int, group_ids: list[int]) -> None:
    ids = resolve_ids(panel, group_ids)  # najpierw sprawdzenie, dopiero potem zmiana przypisań
    panel.db.execute("DELETE FROM app_env_groups WHERE app_id = ?", [app_id])
    for position, group_id in enumerate(ids):
        panel.db.insert("app_env_groups", app_id=app_id, group_id=group_id, position=position)


def forget_app(panel: Panel, app_id: int) -> None:
    panel.db.execute("DELETE FROM app_env_groups WHERE app_id = ?", [app_id])


def effective(panel: Panel, app_id: int, spec: AppSpec) -> AppSpec:
    """Spec do wysłania na węzeł: zmienne zestawów (w kolejności), a na końcu własne zmienne aplikacji."""
    rows = panel.db.fetchall(
        "SELECT g.payload FROM app_env_groups ag JOIN env_groups g ON g.id = ag.group_id WHERE ag.app_id = ? ORDER BY ag.position, ag.group_id",
        [app_id],
    )  # fmt: skip
    layers = [_unseal(panel, row["payload"]) for row in rows] + [(spec.env, spec.env_scopes)]
    env, scopes = merge(layers)
    return spec.model_copy(update={"env": env, "env_scopes": scopes})

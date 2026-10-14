"""Uprawnienia użytkowników (zapis jako ciąg znaków, np. "14D")."""

from __future__ import annotations

from enum import Enum


class Permission(Enum):
    FULL_ACCESS = "0"
    ALLOW_DEPLOYMENT = "1"
    ALLOW_DEPLOYMENT_MODIFICATIONS = "2"
    ALLOW_DEPLOYMENT_DELETE = "3"
    ALLOW_ADD_DOCKERS = "4"
    ALLOW_MODIFY_DOCKERS = "5"
    ALLOW_DELETE_DOCKERS = "6"
    ALLOW_SETTINGS = "7"
    ALLOW_RISKY_SETTINGS = "8"
    ALLOW_CREATING_USERS_SETTING = "9"
    ALLOW_MODIFYING_USERS = "A"
    ALLOW_DELETING_USERS = "B"
    ALLOW_RISKY_PERMISSIONS = "C"
    ALLOW_PULL_IMAGES = "D"
    ALLOW_SEEKING_CONTAINER = "E"
    ALLOW_CREATING_VOLUMES = "F"
    ALLOW_CREATING_NETWORKS = "G"
    ALLOW_DELETING_VOLUMES = "H"
    ALLOW_DELETING_NETWORKS = "I"
    ALLOW_PROXY = "J"
    ALLOW_PROXY_ADVANCED = "K"
    ALLOW_CONTAINER_TERMINAL = "L"
    ALLOW_TUNNEL = "M"
    ALLOW_IMPORT = "N"
    ALLOW_MCP = "O"
    ALLOW_CONTAINER_FILES = "P"
    ALLOW_CONTAINER_FILES_WRITE = "Q"


class _Labels:
    """Etykiety uprawnień z paczki językowej (`perm.<NAZWA>`) — liczone przy każdym użyciu."""

    def __getitem__(self, perm: Permission) -> str:
        from .i18n import t

        return t(f"perm.{perm.name}")

    def items(self) -> list[tuple[Permission, str]]:
        return [(perm, self[perm]) for perm in Permission]


LABELS = _Labels()


GROUPS_SPEC = [
    {
        "id": "admin",
        "icon": "bi-shield-lock",
        "permissions": [
            Permission.FULL_ACCESS,
            Permission.ALLOW_SETTINGS,
            Permission.ALLOW_RISKY_SETTINGS,
            Permission.ALLOW_MCP,
        ],
    },
    {
        "id": "users",
        "icon": "bi-people",
        "permissions": [
            Permission.ALLOW_CREATING_USERS_SETTING,
            Permission.ALLOW_MODIFYING_USERS,
            Permission.ALLOW_DELETING_USERS,
            Permission.ALLOW_RISKY_PERMISSIONS,
        ],
    },
    {
        "id": "apps",
        "icon": "bi-rocket-takeoff",
        "permissions": [
            Permission.ALLOW_DEPLOYMENT,
            Permission.ALLOW_DEPLOYMENT_MODIFICATIONS,
            Permission.ALLOW_DEPLOYMENT_DELETE,
        ],
    },
    {
        "id": "containers",
        "icon": "bi-box",
        "permissions": [
            Permission.ALLOW_ADD_DOCKERS,
            Permission.ALLOW_MODIFY_DOCKERS,
            Permission.ALLOW_DELETE_DOCKERS,
            Permission.ALLOW_PULL_IMAGES,
            Permission.ALLOW_SEEKING_CONTAINER,
        ],
    },
    {
        "id": "files_terminal",
        "icon": "bi-folder2-open",
        "permissions": [
            Permission.ALLOW_CONTAINER_FILES,
            Permission.ALLOW_CONTAINER_FILES_WRITE,
            Permission.ALLOW_CONTAINER_TERMINAL,
        ],
    },
    {
        "id": "storage_network",
        "icon": "bi-hdd-network",
        "permissions": [
            Permission.ALLOW_CREATING_VOLUMES,
            Permission.ALLOW_DELETING_VOLUMES,
            Permission.ALLOW_CREATING_NETWORKS,
            Permission.ALLOW_DELETING_NETWORKS,
        ],
    },
    {
        "id": "gateway_proxy",
        "icon": "bi-signpost-split",
        "permissions": [
            Permission.ALLOW_PROXY,
            Permission.ALLOW_PROXY_ADVANCED,
            Permission.ALLOW_TUNNEL,
            Permission.ALLOW_IMPORT,
        ],
    },
]


class _Groups:
    def __iter__(self):
        from .i18n import t

        for g in GROUPS_SPEC:
            yield {
                "id": g["id"],
                "icon": g["icon"],
                "title": t(f"perm_group.{g['id']}"),
                "permissions": [(p, LABELS[p]) for p in g["permissions"]],
            }


GROUPS = _Groups()


RISKY = {
    Permission.FULL_ACCESS,
    Permission.ALLOW_DEPLOYMENT_DELETE,
    Permission.ALLOW_DELETING_USERS,
    Permission.ALLOW_DELETE_DOCKERS,
    Permission.ALLOW_DELETING_VOLUMES,
    Permission.ALLOW_DELETING_NETWORKS,
    Permission.ALLOW_RISKY_PERMISSIONS,
    Permission.ALLOW_RISKY_SETTINGS,
    Permission.ALLOW_PROXY_ADVANCED,
    Permission.ALLOW_CONTAINER_TERMINAL,
    Permission.ALLOW_CONTAINER_FILES_WRITE,
    Permission.ALLOW_MCP,
    Permission.ALLOW_TUNNEL,
}

_BY_VALUE = {p.value: p for p in Permission}


def parse(value: str | None) -> set[Permission]:
    """Naprawiony parser: stary pomijał litery (A–I), bo sprawdzał `isdigit()`."""
    return {_BY_VALUE[c] for c in (value or "") if c in _BY_VALUE}


def encode(perms: set[Permission]) -> str:
    return "".join(p.value for p in Permission if p in perms)


def has(user: dict | None, perm: Permission) -> bool:
    if not user:
        return False
    granted = parse(user.get("permissions"))
    return Permission.FULL_ACCESS in granted or perm in granted


def grantable(actor: dict, perm: Permission) -> bool:
    """Czy `actor` może nadać to uprawnienie innemu użytkownikowi."""
    if not has(actor, perm):
        return False
    return perm not in RISKY or has(actor, Permission.ALLOW_RISKY_PERMISSIONS)

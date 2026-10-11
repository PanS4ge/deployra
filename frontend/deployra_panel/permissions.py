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


class _Labels:
    """Etykiety uprawnień z paczki językowej (`perm.<NAZWA>`) — liczone przy każdym użyciu."""

    def __getitem__(self, perm: Permission) -> str:
        from .i18n import t

        return t(f"perm.{perm.name}")

    def items(self) -> list[tuple[Permission, str]]:
        return [(perm, self[perm]) for perm in Permission]


LABELS = _Labels()


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

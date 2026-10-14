"""Wykrywanie profilu kontenera i definicje szybkich akcji (bazy danych, cache, webserwery).

Udostępnia lekki, głęboki moduł rozpoznający charakter kontenera (Postgres, MySQL, MariaDB,
Redis, MongoDB, Nginx, Caddy) na podstawie obrazu, zmiennych środowiskowych i portów.
Pozwala na wykonywanie natywnych zrzutów baz (dump) oraz akcji optymalizacyjnych bez
instalowania jakichkolwiek narzędzi na hoście — wszystko działa wewnątrz kontenera przez Docker exec.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime
import re
from typing import Any


@dataclass
class ActionDef:
    id: str
    label: str
    icon: str
    style: str = "ghost"  # "primary", "ghost", "danger", "cool"
    confirm: str | None = None
    is_dump: bool = False
    description: str = ""


@dataclass
class ContainerProfile:
    kind: str  # "postgres", "mysql", "mariadb", "redis", "mongodb", "nginx", "caddy", "generic"
    title: str
    category: str  # "database", "cache", "webserver", "generic"
    icon: str
    color: str
    actions: list[ActionDef] = field(default_factory=list)
    can_dump: bool = False


def _parse_env(raw_env: list[str] | dict[str, str] | None) -> dict[str, str]:
    if not raw_env:
        return {}
    if isinstance(raw_env, dict):
        return dict(raw_env)
    out = {}
    for item in raw_env:
        if "=" in item:
            k, _, v = item.partition("=")
            out[k.strip()] = v.strip()
    return out


def detect_profile(
    image: str = "",
    env: list[str] | dict[str, str] | None = None,
    ports: list[dict[str, Any]] | None = None,
) -> ContainerProfile:
    """Wykrywa profil kontenera na podstawie obrazu, zmiennych środowiskowych i portów."""
    img = (image or "").lower()
    env_map = _parse_env(env)
    ports_set = {p.get("container") or p.get("PrivatePort") for p in ports or [] if (p.get("container") or p.get("PrivatePort"))}

    # 1. PostgreSQL
    if any(k in img for k in ("postgres", "timescale", "postgis")) or 5432 in ports_set or any(k.startswith("POSTGRES_") for k in env_map):
        actions = [
            ActionDef(
                id="dump_sql",
                label="Zrzut bazy (.sql.gz)",
                icon="bi-file-earmark-arrow-down",
                style="primary",
                is_dump=True,
                description="Eksport bazy danych do skompresowanego pliku SQL za pomocą pg_dump",
            ),
            ActionDef(
                id="vacuum",
                label="Optymalizacja (VACUUM)",
                icon="bi-stars",
                description="Odzyskaj nieużywaną pamięć i zaktualizuj statystyki bazy danych (vacuumdb -z)",
            ),
            ActionDef(
                id="db_size",
                label="Rozmiar bazy",
                icon="bi-pie-chart",
                description="Sprawdź rozmiar aktualnej bazy danych",
            ),
        ]
        return ContainerProfile(
            kind="postgres",
            title="PostgreSQL",
            category="database",
            icon="bi-database",
            color="#336791",
            actions=actions,
            can_dump=True,
        )

    # 2. MariaDB
    if "mariadb" in img or any(k.startswith("MARIADB_") for k in env_map):
        actions = [
            ActionDef(
                id="dump_sql",
                label="Zrzut bazy (.sql.gz)",
                icon="bi-file-earmark-arrow-down",
                style="primary",
                is_dump=True,
                description="Eksport wszystkich baz do pliku SQL za pomocą mysqldump",
            ),
            ActionDef(
                id="optimize",
                label="Optymalizacja tabel",
                icon="bi-stars",
                description="Zoptymalizuj tabele i zwolnij miejsce na dysku (mysqlcheck --optimize)",
            ),
            ActionDef(
                id="check",
                label="Sprawdź spójność tabel",
                icon="bi-shield-check",
                description="Szybka kontrola integralności tabel (mysqlcheck --check)",
            ),
        ]
        return ContainerProfile(
            kind="mariadb",
            title="MariaDB",
            category="database",
            icon="bi-database",
            color="#003545",
            actions=actions,
            can_dump=True,
        )

    # 3. MySQL
    if "mysql" in img or 3306 in ports_set or any(k.startswith("MYSQL_") for k in env_map):
        actions = [
            ActionDef(
                id="dump_sql",
                label="Zrzut bazy (.sql.gz)",
                icon="bi-file-earmark-arrow-down",
                style="primary",
                is_dump=True,
                description="Eksport wszystkich baz do pliku SQL za pomocą mysqldump",
            ),
            ActionDef(
                id="optimize",
                label="Optymalizacja tabel",
                icon="bi-stars",
                description="Zoptymalizuj tabele i zwolnij miejsce na dysku (mysqlcheck --optimize)",
            ),
            ActionDef(
                id="check",
                label="Sprawdź spójność tabel",
                icon="bi-shield-check",
                description="Szybka kontrola integralności tabel (mysqlcheck --check)",
            ),
        ]
        return ContainerProfile(
            kind="mysql",
            title="MySQL",
            category="database",
            icon="bi-database",
            color="#f29111",
            actions=actions,
            can_dump=True,
        )

    # 4. Redis / Key-Value
    if any(k in img for k in ("redis", "keydb", "dragonfly")) or 6379 in ports_set:
        actions = [
            ActionDef(
                id="bgsave",
                label="Zapisz na dysk (BGSAVE)",
                icon="bi-floppy",
                description="Zapisz stan pamięci podręcznej do pliku dump.rdb w tle",
            ),
            ActionDef(
                id="flush_cache",
                label="Wyczyść cache (FLUSHDB)",
                icon="bi-trash",
                style="danger",
                confirm="Czy na pewno chcesz usunąć wszystkie klucze z pamięci podręcznej Redis?",
                description="Usuwa wszystkie klucze z bazy Redis, natychmiast uwalniając RAM",
            ),
            ActionDef(
                id="info_memory",
                label="Statystyki RAM",
                icon="bi-memory",
                description="Pokaż szczegółowe zużycie pamięci (redis-cli info memory)",
            ),
        ]
        return ContainerProfile(
            kind="redis",
            title="Redis",
            category="cache",
            icon="bi-lightning-charge",
            color="#dc382d",
            actions=actions,
            can_dump=False,
        )

    # 5. MongoDB
    if "mongo" in img or 27017 in ports_set:
        actions = [
            ActionDef(
                id="dump_archive",
                label="Zrzut bazy (.archive.gz)",
                icon="bi-file-earmark-arrow-down",
                style="primary",
                is_dump=True,
                description="Eksport bazy MongoDB do archiwum gzip za pomocą mongodump",
            ),
            ActionDef(
                id="db_stats",
                label="Statystyki kolekcji",
                icon="bi-pie-chart",
                description="Pokaż statystyki bazy i rozmiary kolekcji",
            ),
        ]
        return ContainerProfile(
            kind="mongodb",
            title="MongoDB",
            category="database",
            icon="bi-database-fill-gear",
            color="#13aa52",
            actions=actions,
            can_dump=True,
        )

    # 6. Nginx
    if "nginx" in img:
        actions = [
            ActionDef(
                id="test_config",
                label="Test konfiguracji (nginx -t)",
                icon="bi-check2-circle",
                description="Sprawdź poprawność składni plików konfiguracyjnych Nginxa",
            ),
            ActionDef(
                id="reload",
                label="Przeładuj bez restartu (reload)",
                icon="bi-arrow-repeat",
                description="Przeładuj konfigurację w locie bez przerywania aktywnych połączeń (nginx -s reload)",
            ),
        ]
        return ContainerProfile(
            kind="nginx",
            title="Nginx",
            category="webserver",
            icon="bi-globe",
            color="#009639",
            actions=actions,
            can_dump=False,
        )

    # 7. Caddy
    if "caddy" in img:
        actions = [
            ActionDef(
                id="test_config",
                label="Test konfiguracji (validate)",
                icon="bi-check2-circle",
                description="Sprawdź poprawność pliku Caddyfile",
            ),
            ActionDef(
                id="reload",
                label="Przeładuj bez restartu (reload)",
                icon="bi-arrow-repeat",
                description="Przeładuj konfigurację Caddy bez zatrzymywania serwera",
            ),
        ]
        return ContainerProfile(
            kind="caddy",
            title="Caddy",
            category="webserver",
            icon="bi-globe",
            color="#1f88e5",
            actions=actions,
            can_dump=False,
        )

    # Domyślny profil ogólny dla każdego kontenera
    generic_actions = [
        ActionDef(
            id="processes",
            label="Procesy w kontenerze",
            icon="bi-cpu",
            description="Wyświetla listę procesów działających wewnątrz kontenera (ps aux)",
        ),
        ActionDef(
            id="disk_usage",
            label="Miejsce na dysku",
            icon="bi-hdd",
            description="Wyświetla zużycie systemu plików kontenera (df -h)",
        ),
    ]
    return ContainerProfile(
        kind="generic",
        title="Kontener",
        category="generic",
        icon="bi-box-seam",
        color="#8b5cf6",
        actions=generic_actions,
        can_dump=False,
    )


def build_action_command(
    profile: ContainerProfile,
    action_id: str,
    env: list[str] | dict[str, str] | None = None,
) -> tuple[list[str], bool, str]:
    """Zwraca (polecenie_exec, czy_to_strumień_pliku, rozszerzenie)."""
    env_map = _parse_env(env)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    # PostgreSQL
    if profile.kind == "postgres":
        user = env_map.get("POSTGRES_USER", "postgres")
        db = env_map.get("POSTGRES_DB", user)
        if action_id == "dump_sql":
            return ["pg_dump", "-U", user, db], True, f"pg_{db}_{ts}.sql.gz"
        if action_id == "vacuum":
            return ["vacuumdb", "-U", user, "-d", db, "-z"], False, ""
        if action_id == "db_size":
            query = "SELECT datname, pg_size_pretty(pg_database_size(datname)) AS size FROM pg_database WHERE NOT datistemplate ORDER BY pg_database_size(datname) DESC;"
            return ["psql", "-U", user, "-d", db, "-c", query], False, ""

    # MySQL / MariaDB
    if profile.kind in ("mysql", "mariadb"):
        user = "root" if ("MYSQL_ROOT_PASSWORD" in env_map or "MARIADB_ROOT_PASSWORD" in env_map) else env_map.get("MYSQL_USER", "root")
        pwd = env_map.get("MYSQL_ROOT_PASSWORD") or env_map.get("MARIADB_ROOT_PASSWORD") or env_map.get("MYSQL_PASSWORD", "")
        db = env_map.get("MYSQL_DATABASE")
        auth_args = [f"-u{user}"]
        if pwd:
            auth_args.append(f"-p{pwd}")

        if action_id == "dump_sql":
            cmd = ["mysqldump"] + auth_args
            if db:
                cmd.append(db)
                filename = f"mysql_{db}_{ts}.sql.gz"
            else:
                cmd.append("--all-databases")
                filename = f"mysql_all_{ts}.sql.gz"
            return cmd, True, filename
        if action_id == "optimize":
            cmd = ["mysqlcheck"] + auth_args + ["--optimize"]
            cmd.append(db if db else "--all-databases")
            return cmd, False, ""
        if action_id == "check":
            cmd = ["mysqlcheck"] + auth_args + ["--check"]
            cmd.append(db if db else "--all-databases")
            return cmd, False, ""

    # Redis
    if profile.kind == "redis":
        pwd = env_map.get("REDIS_PASSWORD") or env_map.get("REQUIREPASS")
        auth_args = ["-a", pwd] if pwd else []
        if action_id == "bgsave":
            return ["redis-cli"] + auth_args + ["bgsave"], False, ""
        if action_id == "flush_cache":
            return ["redis-cli"] + auth_args + ["flushdb"], False, ""
        if action_id == "info_memory":
            return ["redis-cli"] + auth_args + ["info", "memory"], False, ""

    # MongoDB
    if profile.kind == "mongodb":
        if action_id == "dump_archive":
            return ["mongodump", "--archive", "--gzip"], True, f"mongo_{ts}.archive.gz"
        if action_id == "db_stats":
            return ["mongosh", "--eval", "db.stats()"], False, ""

    # Nginx
    if profile.kind == "nginx":
        if action_id == "test_config":
            return ["nginx", "-t"], False, ""
        if action_id == "reload":
            return ["nginx", "-s", "reload"], False, ""

    # Caddy
    if profile.kind == "caddy":
        if action_id == "test_config":
            return ["caddy", "validate"], False, ""
        if action_id == "reload":
            return ["caddy", "reload"], False, ""

    # Generic
    if action_id == "processes":
        return ["sh", "-c", "ps aux 2>/dev/null || ps -ef 2>/dev/null || ps 2>/dev/null"], False, ""
    if action_id == "disk_usage":
        return ["df", "-h"], False, ""

    raise ValueError(f"Nieznana akcja: {action_id} dla profilu {profile.kind}")

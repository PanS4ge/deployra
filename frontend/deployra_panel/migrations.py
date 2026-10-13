"""Migracje schematu (DDL zależny od dialektu: sqlite / mysql)."""

from __future__ import annotations

from collections.abc import Callable


def _types(dialect: str) -> dict[str, str]:
    if dialect == "mysql":
        return {
            "id": "INT AUTO_INCREMENT PRIMARY KEY",
            "text": "MEDIUMTEXT",
            "str": "VARCHAR(190)",
            "int": "INT",
            "real": "DOUBLE",
            "tail": " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
        }
    return {"id": "INTEGER PRIMARY KEY AUTOINCREMENT", "text": "TEXT", "str": "VARCHAR(190)", "int": "INTEGER", "real": "REAL", "tail": ""}


def v1(dialect: str) -> list[str]:
    t = _types(dialect)
    i, s, x, n, r, tail = t["id"], t["str"], t["text"], t["int"], t["real"], t["tail"]
    return [
        f"CREATE TABLE settings (skey {s} PRIMARY KEY, value {x} NOT NULL){tail}",
        f"""CREATE TABLE users (
            id {i}, email {s} NOT NULL UNIQUE, password_hash {x} NOT NULL, name {s}, surname {s},
            permissions {s} NOT NULL DEFAULT '', undeletable {n} NOT NULL DEFAULT 0, created_at {s}
        ){tail}""",
        f"""CREATE TABLE sessions (
            id {s} PRIMARY KEY, user_id {n} NOT NULL, csrf {s} NOT NULL, created_at {s},
            expires_at {r} NOT NULL, ip {s}, user_agent {x}
        ){tail}""",
        f"CREATE TABLE notifications (id {i}, message {x} NOT NULL, level {s} NOT NULL DEFAULT 'info', created_at {s}, is_read {n} NOT NULL DEFAULT 0){tail}",
        f"""CREATE TABLE gateway (
            id {i}, name {s}, bootstrap_url {x}, api_url {x}, secret {x}, public_host {s}, wg_port {n},
            subnet {s}, hub_address {s}, hub_public_key {s}, panel_address {s}, panel_wg_private {x},
            info {x}, created_at {s}
        ){tail}""",
        f"""CREATE TABLE nodes (
            id {i}, name {s} NOT NULL, transport {s} NOT NULL, api_url {x}, secret {x}, tunnel_ip {s},
            peer_id {s}, lan_subnets {x}, status {s} NOT NULL, pair_secret {x}, token_expires {r},
            proxy_address {s}, info {x}, created_at {s}, paired_at {s}
        ){tail}""",
        f"""CREATE TABLE certificates (
            id {i}, name {s}, domains {x} NOT NULL, challenge {s} NOT NULL, status {s} NOT NULL,
            error {x}, expires_at {s}, last_job {s}, created_at {s}
        ){tail}""",
        f"""CREATE TABLE access_lists (
            id {i}, name {s} NOT NULL, satisfy_any {n} NOT NULL DEFAULT 0, pass_auth {n} NOT NULL DEFAULT 0,
            users {x}, clients {x}, created_at {s}
        ){tail}""",
        f"""CREATE TABLE sites (
            id {i}, kind {s} NOT NULL, domains {x} NOT NULL, target {x}, redirect {x}, locations {x},
            cert_id {n}, ssl_forced {n} NOT NULL DEFAULT 0, http2 {n} NOT NULL DEFAULT 1, hsts {n} NOT NULL DEFAULT 0,
            hsts_subdomains {n} NOT NULL DEFAULT 0, block_exploits {n} NOT NULL DEFAULT 1, websockets {n} NOT NULL DEFAULT 1,
            caching {n} NOT NULL DEFAULT 0, trust_forwarded_proto {n} NOT NULL DEFAULT 0, access_list_id {n},
            advanced {x}, max_body_mb {n} NOT NULL DEFAULT 100, enabled {n} NOT NULL DEFAULT 1, app_id {n},
            source {s} NOT NULL DEFAULT 'manual', dns {n} NOT NULL DEFAULT 0, created_at {s}
        ){tail}""",
        f"""CREATE TABLE streams (
            id {i}, listen_port {n} NOT NULL, node_id {n}, upstream_host {s} NOT NULL, upstream_port {n} NOT NULL,
            tcp {n} NOT NULL DEFAULT 1, udp {n} NOT NULL DEFAULT 0, enabled {n} NOT NULL DEFAULT 1, created_at {s}
        ){tail}""",
        f"""CREATE TABLE apps (
            id {i}, slug {s} NOT NULL UNIQUE, name {s} NOT NULL, node_id {n} NOT NULL, kind {s} NOT NULL,
            spec {x} NOT NULL, domains {x}, site_id {n}, cert_id {n}, status {s} NOT NULL, host_port {n},
            webhook_secret {x}, last_job {s}, last_deploy_at {s}, last_error {x}, created_at {s}
        ){tail}""",
        f"CREATE TABLE dns_records (id {i}, site_id {n}, domain {s} NOT NULL, zone_id {s}, record_id {s}, created_at {s}){tail}",
    ]


def v2(dialect: str) -> list[str]:
    """Uptime: monitory, surowe sprawdzenia (48 h) i agregaty godzinowe (90 dni)."""
    t = _types(dialect)
    i, s, x, n, r, tail = t["id"], t["str"], t["text"], t["int"], t["real"], t["tail"]
    return [
        f"""CREATE TABLE monitors (
            id {i}, name {s} NOT NULL, kind {s} NOT NULL, target {x}, ref_id {n}, node_id {n},
            interval_s {n} NOT NULL DEFAULT 60, enabled {n} NOT NULL DEFAULT 1, status {s} NOT NULL DEFAULT 'unknown',
            last_check {r}, last_change {r}, last_message {x}, created_at {s}
        ){tail}""",
        f"CREATE TABLE monitor_checks (id {i}, monitor_id {n} NOT NULL, ts {r} NOT NULL, ok {n} NOT NULL, latency_ms {r}, message {x}){tail}",
        "CREATE INDEX idx_monitor_checks ON monitor_checks (monitor_id, ts)",
        f"""CREATE TABLE monitor_hourly (
            monitor_id {n} NOT NULL, hour {n} NOT NULL, checks {n} NOT NULL DEFAULT 0, ups {n} NOT NULL DEFAULT 0,
            latency_sum {r} NOT NULL DEFAULT 0, latency_n {n} NOT NULL DEFAULT 0, PRIMARY KEY (monitor_id, hour)
        ){tail}""",
    ]


def v3(dialect: str) -> list[str]:
    """Ruch przychodzący per host: publiczne IP bramki albo Cloudflare Tunnel (cloudflared)."""
    return ["ALTER TABLE sites ADD COLUMN ingress VARCHAR(16) NOT NULL DEFAULT 'public'"]


def v4(dialect: str) -> list[str]:
    """Język interfejsu wybrany przez użytkownika (NULL = domyślny panelu / przeglądarki)."""
    return ["ALTER TABLE users ADD COLUMN lang VARCHAR(8)"]


def v5(dialect: str) -> list[str]:
    """Zasoby: próbki co minutę (48 h) i agregaty godzinowe (30 dni) dla węzłów i aplikacji; kanały powiadomień i alerty."""
    t = _types(dialect)
    i, s, x, n, r, tail = t["id"], t["str"], t["text"], t["int"], t["real"], t["tail"]
    return [
        f"""CREATE TABLE node_samples (
            id {i}, node_id {n} NOT NULL, ts {r} NOT NULL, cpu {r}, ram {r}, swap {r}, disk {r}, load1 {r},
            ram_used {r}, ram_total {r}, disk_used {r}, disk_total {r}
        ){tail}""",
        "CREATE INDEX idx_node_samples ON node_samples (node_id, ts)",
        f"""CREATE TABLE node_hourly (
            node_id {n} NOT NULL, hour {n} NOT NULL, n {n} NOT NULL DEFAULT 0, cpu_sum {r} NOT NULL DEFAULT 0, cpu_max {r} NOT NULL DEFAULT 0,
            ram_sum {r} NOT NULL DEFAULT 0, ram_max {r} NOT NULL DEFAULT 0, disk_max {r} NOT NULL DEFAULT 0, PRIMARY KEY (node_id, hour)
        ){tail}""",
        f"CREATE TABLE app_samples (id {i}, app_id {n} NOT NULL, ts {r} NOT NULL, cpu {r}, mem {r}, mem_limit {r}){tail}",
        "CREATE INDEX idx_app_samples ON app_samples (app_id, ts)",
        f"""CREATE TABLE app_hourly (
            app_id {n} NOT NULL, hour {n} NOT NULL, n {n} NOT NULL DEFAULT 0, cpu_sum {r} NOT NULL DEFAULT 0, cpu_max {r} NOT NULL DEFAULT 0,
            mem_sum {r} NOT NULL DEFAULT 0, mem_max {r} NOT NULL DEFAULT 0, PRIMARY KEY (app_id, hour)
        ){tail}""",
        f"""CREATE TABLE notify_channels (
            id {i}, name {s} NOT NULL, kind {s} NOT NULL, config {x}, enabled {n} NOT NULL DEFAULT 1, created_at {s}
        ){tail}""",
        f"""CREATE TABLE alerts (
            id {i}, rule {s} NOT NULL, target_kind {s} NOT NULL, target_id {n}, target_name {s}, status {s} NOT NULL,
            message {x}, value {r}, started_at {r} NOT NULL, resolved_at {r}
        ){tail}""",
        "CREATE INDEX idx_alerts ON alerts (status, started_at)",
    ]


def v6(dialect: str) -> list[str]:
    """Bramka brzegowa („widelec”): rola bramki (hub WireGuard / brzeg z ruchem publicznym) i oczekujące dołączenie."""
    x = _types(dialect)["text"]
    return [
        "ALTER TABLE gateway ADD COLUMN role VARCHAR(16) NOT NULL DEFAULT 'hub'",
        "ALTER TABLE gateway ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT 'paired'",
        f"ALTER TABLE gateway ADD COLUMN pair_secret {x}",
        "ALTER TABLE gateway ADD COLUMN token_id VARCHAR(64)",
    ]


def v7(dialect: str) -> list[str]:
    """Konta „tylko przypisane serwery” (w stylu Pterodactyla) i przypisania aplikacji/kontenerów z uprawnieniami."""
    t = _types(dialect)
    i, s, n, tail = t["id"], t["str"], t["int"], t["tail"]
    return [
        "ALTER TABLE users ADD COLUMN scoped INTEGER NOT NULL DEFAULT 0",
        f"""CREATE TABLE grants (
            id {i}, user_id {n} NOT NULL, kind {s} NOT NULL, node_id {n}, app_id {n}, container {s},
            perms {s} NOT NULL DEFAULT '', created_at {s}
        ){tail}""",
        "CREATE INDEX idx_grants_user ON grants (user_id)",
    ]


def v8(dialect: str) -> list[str]:
    """Archiwum hostów proxy: wyłączone i schowane z listy, do przywrócenia jednym kliknięciem."""
    return ["ALTER TABLE sites ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"]


def v9(dialect: str) -> list[str]:
    """Wyciszanie alertów: data zignorowania alertu."""
    t = _types(dialect)
    r = t["real"]
    return [f"ALTER TABLE alerts ADD COLUMN ignored_at {r}"]


MIGRATIONS: list[tuple[int, Callable[[str], list[str]]]] = [(1, v1), (2, v2), (3, v3), (4, v4), (5, v5), (6, v6), (7, v7), (8, v8), (9, v9)]

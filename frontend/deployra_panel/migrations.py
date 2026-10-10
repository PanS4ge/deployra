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


MIGRATIONS: list[tuple[int, Callable[[str], list[str]]]] = [(1, v1)]

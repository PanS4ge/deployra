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


def v10(dialect: str) -> list[str]:
    """Trwała historia zadań w panelu: wdrożenia, aktualizacje, certyfikaty."""
    t = _types(dialect)
    s, x, r, tail = t["str"], t["text"], t["real"], t["tail"]
    return [
        f"""CREATE TABLE jobs (
            id VARCHAR(32) PRIMARY KEY, kind {s} NOT NULL, title {s} NOT NULL, status {s} NOT NULL,
            error {x}, result {x}, lines {x}, created {r} NOT NULL, finished {r}
        ){tail}""",
        "CREATE INDEX idx_jobs_created ON jobs (created)",
    ]


def v11(dialect: str) -> list[str]:
    """Rejestry Docker: prywatne rejestry obrazów (Docker Hub, GHCR, GitLab, Harbor)."""
    t = _types(dialect)
    i, s, x, tail = t["id"], t["str"], t["text"], t["tail"]
    return [
        f"""CREATE TABLE registries (
            id {i}, name {s} NOT NULL, url {s} NOT NULL, username {s} NOT NULL,
            password {x} NOT NULL, created_at {s}
        ){tail}"""
    ]


def v12(dialect: str) -> list[str]:
    """Tokeny API / MCP: klucze dostępu dla agentów AI i narzędzi zewnętrznych."""
    t = _types(dialect)
    i, s, x, n, r, tail = t["id"], t["str"], t["text"], t["int"], t["real"], t["tail"]
    return [
        f"""CREATE TABLE api_tokens (
            id {i}, name {s} NOT NULL, token_hash {s} NOT NULL UNIQUE, prefix {s} NOT NULL,
            user_id {n} NOT NULL, permissions {s} NOT NULL DEFAULT '', created_at {s}, last_used_at {r}
        ){tail}""",
        "CREATE INDEX idx_api_tokens_hash ON api_tokens (token_hash)",
    ]


def v13(dialect: str) -> list[str]:
    """Bramka bez WireGuarda (czysty nginx), przypisanie bramki do rejestracji i hostów proxy."""
    t = _types(dialect)
    n = t["int"]
    return [
        "ALTER TABLE gateway ADD COLUMN wg_enabled INTEGER NOT NULL DEFAULT 1",
        f"ALTER TABLE sites ADD COLUMN gateway_id {n}",
        f"ALTER TABLE nodes ADD COLUMN gateway_id {n}",
    ]


def v14(dialect: str) -> list[str]:
    """Własna ikona aplikacji (`url:…` albo `file:…`; pusta = automatycznie: favicon → logo obrazu)."""
    return [f"ALTER TABLE apps ADD COLUMN icon {_types(dialect)['text']}"]


def v15(dialect: str) -> list[str]:
    """Serwery Minecraft z pluginem DeployraBridge (parowanie kodem, sekret zaszyfrowany kluczem panelu)."""
    t = _types(dialect)
    i, s, x, n, r, tail = t["id"], t["str"], t["text"], t["int"], t["real"], t["tail"]
    return [
        f"""CREATE TABLE minecraft_servers (
            id {i}, name {s} NOT NULL, app_id {n}, secret {x}, pair_hash {s}, pair_code {x}, pair_expires {r}, ftp {x}, macros {x},
            info {x}, last_seen {r}, created_at {s}
        ){tail}""",
        "CREATE INDEX idx_minecraft_pair ON minecraft_servers (pair_hash)",
    ]


def v16(dialect: str) -> list[str]:
    """Ręczne limity zasobów węzła — kontenery LXC (np. mikr.us) bez lxcfs widzą RAM całego serwera fizycznego."""
    n = _types(dialect)["int"]
    return [f"ALTER TABLE nodes ADD COLUMN ram_limit_mb {n}", f"ALTER TABLE nodes ADD COLUMN disk_limit_gb {n}"]


def v17(dialect: str) -> list[str]:
    """Zadania cykliczne w kontenerach aplikacji: harmonogram (cron), polecenie i historia uruchomień (ostatnie 50)."""
    t = _types(dialect)
    i, s, x, n, r, tail = t["id"], t["str"], t["text"], t["int"], t["real"], t["tail"]
    return [
        f"""CREATE TABLE scheduled_tasks (
            id {i}, app_id {n} NOT NULL, name {s} NOT NULL, command {x} NOT NULL, schedule {s} NOT NULL,
            timeout_s {n} NOT NULL DEFAULT 300, enabled {n} NOT NULL DEFAULT 1, last_run_at {r}, last_status {s}, created_at {s}
        ){tail}""",
        "CREATE INDEX idx_scheduled_tasks_app ON scheduled_tasks (app_id)",
        f"""CREATE TABLE scheduled_task_runs (
            id {i}, task_id {n} NOT NULL, started_at {r} NOT NULL, duration_s {r}, exit_code {n}, status {s} NOT NULL,
            output {x}
        ){tail}""",
        "CREATE INDEX idx_scheduled_task_runs ON scheduled_task_runs (task_id, started_at)",
    ]


def v18(dialect: str) -> list[str]:
    """Harmonogram kopii baz danych: zrzut kontenera bazy (pg_dump / mysqldump / mongodump) do magazynu kopii."""
    t = _types(dialect)
    i, s, x, n, r, tail = t["id"], t["str"], t["text"], t["int"], t["real"], t["tail"]
    return [
        f"""CREATE TABLE db_backup_schedules (
            id {i}, node_id {n} NOT NULL, container {s} NOT NULL, folder {s} NOT NULL, cron {s} NOT NULL,
            retention {n} NOT NULL DEFAULT 7, enabled {n} NOT NULL DEFAULT 1, last_run {r}, last_status {s},
            last_message {x}, last_filename {s}, last_size {r}, created_at {s}
        ){tail}"""
    ]


def v19(dialect: str) -> list[str]:
    """Wdrożenie bez przestojów (opcjonalne) i ścieżka testu zdrowia — kopia ustawień z zapisanej specyfikacji aplikacji."""
    t = _types(dialect)
    return [f"ALTER TABLE apps ADD COLUMN zero_downtime {t['int']} NOT NULL DEFAULT 0", f"ALTER TABLE apps ADD COLUMN health_path {t['str']}"]


def v20(dialect: str) -> list[str]:
    """Logowanie Deployra przed aplikacją (forward auth): włącznik na liście dostępu i lista dozwolonych użytkowników."""
    t = _types(dialect)
    n, x = t["int"], t["text"]
    return [
        f"ALTER TABLE access_lists ADD COLUMN deployra_login {n} NOT NULL DEFAULT 0",
        f"ALTER TABLE access_lists ADD COLUMN login_users {x}",
    ]


def v22(dialect: str) -> list[str]:
    """HTTP/3 (QUIC) per host proxy — domyślnie wyłączony."""
    return [f"ALTER TABLE sites ADD COLUMN http3 {_types(dialect)['int']} NOT NULL DEFAULT 0"]


def v21(dialect: str) -> list[str]:
    """Zmienne współdzielone: nazwane zestawy zmiennych (zaszyfrowane) i ich dołączenie do aplikacji w kolejności."""
    t = _types(dialect)
    i, s, x, n, tail = t["id"], t["str"], t["text"], t["int"], t["tail"]
    return [
        f"CREATE TABLE env_groups (id {i}, name {s} NOT NULL UNIQUE, payload {x} NOT NULL, created_at {s}){tail}",
        f"CREATE TABLE app_env_groups (app_id {n} NOT NULL, group_id {n} NOT NULL, position {n} NOT NULL DEFAULT 0, PRIMARY KEY (app_id, group_id)){tail}",
        "CREATE INDEX idx_app_env_groups_group ON app_env_groups (group_id)",
    ]


def v23(dialect: str) -> list[str]:
    """Węzeł za zewnętrznym WireGuardem (wg-easy itp.): adres i podsieć WG, kontener dla `network_mode`, adres dla bramki."""
    return [f"ALTER TABLE nodes ADD COLUMN external_wg {_types(dialect)['text']}"]


def v24(dialect: str) -> list[str]:
    """Podatności: paczka w kontenerze/aplikacji z listą podatności z OSV.dev i pamięć podręczna wpisów OSV."""
    t = _types(dialect)
    i, s, x, n, r, tail = t["id"], t["str"], t["text"], t["int"], t["real"], t["tail"]
    return [
        f"""CREATE TABLE vuln_findings (
            id {i}, node_id {n} NOT NULL, endpoint {s} NOT NULL, container {s} NOT NULL, app_id {n}, label {s} NOT NULL,
            ecosystem {s} NOT NULL, package {x} NOT NULL, version {s} NOT NULL, source {x}, severity {s} NOT NULL, score {r},
            vulns {x} NOT NULL, fixed {s}, kev {n} NOT NULL DEFAULT 0, status {s} NOT NULL, ignored_ids {x},
            first_seen {r} NOT NULL, last_seen {r} NOT NULL, resolved_at {r}
        ){tail}""",
        "CREATE INDEX idx_vuln_findings_status ON vuln_findings (status)",
        f"CREATE TABLE vuln_cache (id {s} PRIMARY KEY, modified {s}, data {x} NOT NULL, fetched_at {r} NOT NULL){tail}",
    ]


def v25(dialect: str) -> list[str]:
    """Aktualizacje obrazów aplikacji: tryb per aplikacja (pusty — jak w ustawieniach) i znalezione aktualizacje."""
    t = _types(dialect)
    s, x, n, r, tail = t["str"], t["text"], t["int"], t["real"], t["tail"]
    return [
        f"ALTER TABLE apps ADD COLUMN update_policy {s}",
        f"CREATE TABLE app_updates (app_id {n} PRIMARY KEY, items {x} NOT NULL, notified {s}, checked_at {r} NOT NULL){tail}",
    ]


MIGRATIONS: list[tuple[int, Callable[[str], list[str]]]] = [
    (1, v1), (2, v2), (3, v3), (4, v4), (5, v5), (6, v6), (7, v7), (8, v8), (9, v9), (10, v10), (11, v11), (12, v12), (13, v13),
    (14, v14), (15, v15), (16, v16), (17, v17), (18, v18), (19, v19), (20, v20), (21, v21), (22, v22), (23, v23), (24, v24), (25, v25),
]

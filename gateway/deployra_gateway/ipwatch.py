"""Ruch per adres IP: topka adresów i podejrzane IP (zalew zapytań, skanery) — podstawa do banów.

Te same logi nginx co statystyki odwiedzin (syslog/UDP), ale tu liczymy po surowym IP, bo do
banowania potrzebny jest adres. Pamięć jest ograniczona: w kubełku minuty i w dniu trzymamy
najaktywniejsze adresy (resztę przycinamy), a na dysk trafia PERSIST_TOP adresów dnia,
przechowywanych `retention_days` dni (domyślnie 7 — adres IP to dana osobowa).
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# typowe ścieżki, które sprawdzają skanery podatności
PROBES = re.compile(
    r"/\.env|/\.git/|wp-login\.php|xmlrpc\.php|phpmyadmin|/pma/|/cgi-bin/|/boaform|/hnap1|/vendor/phpunit|/actuator|"
    r"/\.aws/|/server-status|/owa/|/autodiscover|/\.ds_store|/eval-stdin|/wp-admin/install|/setup\.php|/shell\b|/\.ssh/",
    re.IGNORECASE,
)
MINUTE_CAP = 3000
DAY_CAP = 5000
PERSIST_TOP = 500
WINDOW_MINUTES = 5
MAX_DOMAINS = 8
MAX_PATHS = 5

# indeksy w liście statystyk adresu
REQ, BYTES, ERR4, ERR5, PROBE = range(5)


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _prune(table: dict[str, list], cap: int) -> None:
    """Zostawia `cap` najaktywniejszych adresów (wołane, gdy słownik urośnie do 2 × cap)."""
    keep = sorted(table.items(), key=lambda item: item[1][REQ], reverse=True)[:cap]
    table.clear()
    table.update(keep)


class IpWatch:
    def __init__(self, data_dir: Path, retention_days: int = 7) -> None:
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(data_dir / "ipwatch.sqlite"), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS ip_daily (day TEXT NOT NULL, ip TEXT NOT NULL, requests INTEGER, bytes INTEGER, "
            "err4 INTEGER, err5 INTEGER, probes INTEGER, last REAL, domains TEXT, PRIMARY KEY (day, ip))"
        )
        now = time.time()
        self.minute = int(now // 60)
        self.current: dict[str, list[int]] = {}
        self.window: deque[tuple[int, dict[str, list[int]]]] = deque(maxlen=WINDOW_MINUTES)
        self.day = _day(now)
        self.today: dict[str, list] = {}  # ip → [req, bytes, err4, err5, probes, last, {domena: n}]
        self.paths: dict[str, list[str]] = {}
        self.samples: dict[str, list[dict[str, Any]]] = {}
        self._load_today()

    # --- zbieranie ----------------------------------------------------------------------

    def ingest(self, record: dict[str, Any], ts: float | None = None) -> None:
        ip = str(record.get("a") or "").strip()[:45]
        if not ip:
            return
        ts = ts or float(record.get("t") or time.time())
        try:
            status = int(record.get("s") or 0)
            size = int(record.get("b") or 0)
        except (TypeError, ValueError):
            status, size = 0, 0
        domain = str(record.get("h") or "_").lower().strip(".")[:253] or "_"
        path = str(record.get("r") or "")[:200]
        method = str(record.get("m") or "GET").upper()[:10]
        ua = str(record.get("u") or "")[:120]
        probe = bool(path) and PROBES.search(path) is not None
        with self._lock:
            self._roll(ts)
            minute = self.current.get(ip)
            if minute is None:
                minute = self.current[ip] = [0, 0, 0, 0, 0]
                if len(self.current) > MINUTE_CAP * 2:
                    _prune(self.current, MINUTE_CAP)
                    minute = self.current.setdefault(ip, [0, 0, 0, 0, 0])
            stats = self.today.get(ip)
            if stats is None:
                stats = self.today[ip] = [0, 0, 0, 0, 0, 0.0, {}]
                if len(self.today) > DAY_CAP * 2:
                    _prune(self.today, DAY_CAP)
                    stats = self.today.setdefault(ip, [0, 0, 0, 0, 0, 0.0, {}])
            for row in (minute, stats):
                row[REQ] += 1
                row[BYTES] += size
                if 400 <= status < 500:
                    row[ERR4] += 1
                elif status >= 500:
                    row[ERR5] += 1
                if probe:
                    row[PROBE] += 1
            stats[5] = ts
            domains = stats[6]
            if domain in domains or len(domains) < MAX_DOMAINS:
                domains[domain] = domains.get(domain, 0) + 1
            if probe:
                seen = self.paths.setdefault(ip, [])
                if path not in seen:
                    seen.append(path)
                    del seen[:-MAX_PATHS]
            sample_list = self.samples.setdefault(ip, [])
            sample_list.append({
                "time": ts,
                "method": method,
                "path": path or "/",
                "status": status,
                "domain": domain,
                "bytes": size,
                "ua": ua,
            })
            if len(sample_list) > 12:
                del sample_list[:-12]
            if len(self.samples) > DAY_CAP * 2:
                active_ips = set(self.today)
                self.samples = {k: v for k, v in self.samples.items() if k in active_ips}

    def _roll(self, ts: float) -> None:
        minute = int(ts // 60)
        if minute > self.minute:  # spóźnione pakiety liczymy do bieżącej minuty
            self.window.append((self.minute, self.current))
            self.minute, self.current = minute, {}
        day = _day(ts)
        if day > self.day:
            self._persist()
            self.day, self.today, self.paths, self.samples = day, {}, {}, {}
            self.db.execute("DELETE FROM ip_daily WHERE day < ?", [_day(ts - self.retention_days * 86400)])

    # --- trwałość -------------------------------------------------------------------------

    def _persist(self) -> None:
        top = sorted(self.today.items(), key=lambda item: item[1][REQ], reverse=True)[:PERSIST_TOP]
        self.db.execute("DELETE FROM ip_daily WHERE day = ?", [self.day])
        self.db.executemany(
            "INSERT INTO ip_daily (day, ip, requests, bytes, err4, err5, probes, last, domains) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(self.day, ip, s[REQ], s[BYTES], s[ERR4], s[ERR5], s[PROBE], s[5], json.dumps(s[6])) for ip, s in top],
        )

    def _load_today(self) -> None:
        for ip, req, size, err4, err5, probes, last, domains in self.db.execute(
            "SELECT ip, requests, bytes, err4, err5, probes, last, domains FROM ip_daily WHERE day = ?", [self.day]
        ):
            self.today[ip] = [req, size, err4, err5, probes, last, json.loads(domains or "{}")]

    def flush(self) -> None:
        with self._lock:
            self._roll(time.time())
            self._persist()

    # --- odczyt ---------------------------------------------------------------------------

    def top(self, days: int = 1, domain: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Najaktywniejsze adresy z ostatnich `days` dni (dziś z pamięci, wcześniej z bazy)."""
        days = max(1, min(days, self.retention_days))
        merged: dict[str, list] = {}

        def add(ip: str, row: list) -> None:
            domains = row[6]
            if domain and domain not in domains:
                return
            entry = merged.setdefault(ip, [0, 0, 0, 0, 0, 0.0, {}])
            for i in range(5):
                entry[i] += row[i]
            entry[5] = max(entry[5], row[5] or 0)
            for name, count in domains.items():
                entry[6][name] = entry[6].get(name, 0) + count

        with self._lock:
            self._roll(time.time())
            since = _day(time.time() - (days - 1) * 86400)
            rows = self.db.execute(
                "SELECT ip, requests, bytes, err4, err5, probes, last, domains FROM ip_daily WHERE day >= ? AND day < ?", [since, self.day]
            ).fetchall()
            for ip, req, size, err4, err5, probes, last, domains in rows:
                add(ip, [req, size, err4, err5, probes, last, json.loads(domains or "{}")])
            for ip, row in self.today.items():
                add(ip, row)
            paths = {ip: list(found) for ip, found in self.paths.items()}
            samples = {ip: list(found) for ip, found in self.samples.items()}

        def requests(row: list) -> int:
            return row[6].get(domain, 0) if domain else row[REQ]

        out = []
        for ip, row in sorted(merged.items(), key=lambda item: requests(item[1]), reverse=True)[:limit]:
            out.append({
                "ip": ip, "requests": requests(row), "bytes": row[BYTES], "err4": row[ERR4], "err5": row[ERR5],
                "probes": row[PROBE], "last": row[5], "paths": paths.get(ip, []),
                "domains": [name for name, _ in sorted(row[6].items(), key=lambda kv: kv[1], reverse=True)[:3]],
                "samples": samples.get(ip, []),
            })  # fmt: skip
        return out

    def suspicious(self, rate_limit: int = 600, now: float | None = None) -> list[dict[str, Any]]:
        """Adresy, które w ostatnich minutach zalewały zapytaniami, skanowały albo szukały dziur.

        Powody to kody (`rate`, `scan`, `probes`) z wartościami — panel tłumaczy je na język użytkownika.
        """
        now = now or time.time()
        with self._lock:
            self._roll(now)
            first = int(now // 60) - WINDOW_MINUTES
            buckets = [bucket for minute, bucket in self.window if minute >= first] + [self.current]
            totals: dict[str, list[int]] = {}
            peaks: dict[str, int] = {}
            for bucket in buckets:
                for ip, row in bucket.items():
                    total = totals.setdefault(ip, [0, 0, 0, 0, 0])
                    for i in range(5):
                        total[i] += row[i]
                    peaks[ip] = max(peaks.get(ip, 0), row[REQ])
            paths = {ip: list(found) for ip, found in self.paths.items()}
            domains = {ip: list(self.today[ip][6])[:3] if ip in self.today else [] for ip in totals}
            samples = {ip: list(found) for ip, found in self.samples.items()}
        out = []
        for ip, row in totals.items():
            reasons = []
            if peaks[ip] >= rate_limit:
                reasons.append({"code": "rate", "value": peaks[ip]})
            if row[REQ] >= 30 and row[ERR4] / row[REQ] >= 0.8:
                reasons.append({"code": "scan", "value": round(100 * row[ERR4] / row[REQ])})
            if row[PROBE] >= 3:
                reasons.append({"code": "probes", "value": row[PROBE]})
            if reasons:
                out.append({
                    "ip": ip, "reasons": reasons, "severity": "high" if reasons[0]["code"] == "rate" else "medium",
                    "requests": row[REQ], "peak": peaks[ip], "err4": row[ERR4], "paths": paths.get(ip, []), "domains": domains.get(ip, []),
                    "samples": samples.get(ip, []),
                })  # fmt: skip
        out.sort(key=lambda item: (item["severity"] != "high", -item["peak"]))
        return out

"""Statystyki odwiedzin per domena: zapytania, unikalni i powracający odwiedzający.

nginx wysyła log każdego żądania przez syslog/UDP na 127.0.0.1 (bez plików
i zapisu na dysk). Odwiedzającego rozpoznajemy po skrócie (IP + User-Agent +
sekret bramki) — bez ciasteczek, nic do zgody RODO. Unikalni = różne skróty
w danym dniu (dokładny zbiór w pamięci); powracający = odwiedzający, którego
widzieliśmy w poprzednich dniach (dwa rotowane filtry Blooma ≈ 30 dni).
Boty i health-checki są pomijane w liczeniu odwiedzających.

RAM: ~80 B na unikalnego odwiedzającego dziennie + 2 × 512 KB filtrów Blooma.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import sqlite3
import threading
import time
from array import array
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from deployra_common.i18n import tr

BOTS = re.compile(
    r"bot|crawl|spider|slurp|curl|wget|python|httpx|aiohttp|go-http|java/|okhttp|libwww|scrapy|monitor|uptime|deployra|"
    r"headless|lighthouse|facebookexternalhit|preview|feedfetcher|nmap|masscan|zgrab",
    re.IGNORECASE,
)
BLOOM_BITS = 1 << 22  # 4 Mi bitów = 512 KB, ~440 tys. wpisów przy 1% fałszywych trafień
BLOOM_HASHES = 7
BLOOM_ROTATE_DAYS = 15
HOURLY_KEEP_HOURS = 72
DAILY_KEEP_DAYS = 400


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


class Bloom:
    def __init__(self, data: bytes | None = None, created: float | None = None) -> None:
        self.bits = bytearray(data) if data and len(data) == BLOOM_BITS // 8 else bytearray(BLOOM_BITS // 8)
        self.created = created or time.time()

    def _positions(self, key: bytes):
        digest = hashlib.blake2b(key, digest_size=16).digest()
        a, b = int.from_bytes(digest[:8], "little"), int.from_bytes(digest[8:], "little") | 1
        for i in range(BLOOM_HASHES):
            yield (a + i * b) % BLOOM_BITS

    def add(self, key: bytes) -> None:
        for pos in self._positions(key):
            self.bits[pos >> 3] |= 1 << (pos & 7)

    def __contains__(self, key: bytes) -> bool:
        return all(self.bits[pos >> 3] & (1 << (pos & 7)) for pos in self._positions(key))


class Analytics:
    def __init__(self, data_dir: Path, secret: bytes) -> None:
        self.dir = data_dir
        self.secret = secret
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(data_dir / "analytics.sqlite"), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS daily (domain TEXT NOT NULL, day TEXT NOT NULL, requests INTEGER NOT NULL DEFAULT 0,
                uniques INTEGER NOT NULL DEFAULT 0, returners INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (domain, day));
            CREATE TABLE IF NOT EXISTS hourly (domain TEXT NOT NULL, hour INTEGER NOT NULL, requests INTEGER NOT NULL DEFAULT 0,
                uniques INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (domain, hour));
            """
        )
        self.day = _day(time.time())
        self.hour = int(time.time() // 3600)
        self.today: dict[str, set[int]] = {}
        self.returning: dict[str, int] = {}
        self.requests: dict[str, int] = {}
        self.bytes: dict[str, int] = {}
        self.hour_sets: dict[str, set[int]] = {}
        self.hour_requests: dict[str, int] = {}
        self.bloom_cur, self.bloom_prev = self._load_blooms()
        self._load_today()
        self.received = 0

    # --- trwałość -----------------------------------------------------------------------

    def _load_blooms(self) -> tuple[Bloom, Bloom]:
        meta_path = self.dir / "analytics-bloom.json"
        try:
            meta = json.loads(meta_path.read_text())
            cur = Bloom((self.dir / "analytics-bloom-cur.bin").read_bytes(), meta["cur"])
            prev = Bloom((self.dir / "analytics-bloom-prev.bin").read_bytes(), meta["prev"])
            return cur, prev
        except (OSError, ValueError, KeyError):
            return Bloom(), Bloom()

    def _save_blooms(self) -> None:
        (self.dir / "analytics-bloom-cur.bin").write_bytes(bytes(self.bloom_cur.bits))
        (self.dir / "analytics-bloom-prev.bin").write_bytes(bytes(self.bloom_prev.bits))
        (self.dir / "analytics-bloom.json").write_text(json.dumps({"cur": self.bloom_cur.created, "prev": self.bloom_prev.created}))

    def _load_today(self) -> None:
        try:
            data = json.loads((self.dir / "analytics-today.json").read_text())
        except (OSError, ValueError):
            data = {}
        if data.get("day") != self.day:
            return
        for domain, info in data.get("domains", {}).items():
            hashes = array("Q")
            hashes.frombytes(base64.b64decode(info["hashes"]))
            self.today[domain] = set(hashes)
            self.returning[domain] = info.get("returning", 0)
            self.requests[domain] = info.get("requests", 0)
            self.bytes[domain] = info.get("bytes", 0)

    def save_today(self) -> None:
        with self._lock:
            payload = {
                "day": self.day,
                "domains": {
                    d: {
                        "hashes": base64.b64encode(array("Q", sorted(s)).tobytes()).decode(),
                        "returning": self.returning.get(d, 0),
                        "requests": self.requests.get(d, 0),
                        "bytes": self.bytes.get(d, 0),
                    }
                    for d, s in self.today.items()
                },
            }
        tmp = self.dir / "analytics-today.json.tmp"
        tmp.write_text(json.dumps(payload))
        tmp.replace(self.dir / "analytics-today.json")

    # --- zliczanie ----------------------------------------------------------------------

    def _visitor(self, ip: str, ua: str) -> int:
        digest = hashlib.blake2b(f"{ip}\n{ua}".encode(), key=self.secret, digest_size=8).digest()
        return int.from_bytes(digest, "little")

    def ingest(self, record: dict[str, Any], ts: float | None = None) -> None:
        ts = ts or float(record.get("t") or time.time())
        domain = str(record.get("h") or "").lower().strip(".")[:253]
        if not domain or domain == "_":
            return
        with self._lock:
            self._roll(ts)
            self.received += 1
            self.requests[domain] = self.requests.get(domain, 0) + 1
            self.hour_requests[domain] = self.hour_requests.get(domain, 0) + 1
            try:
                self.bytes[domain] = self.bytes.get(domain, 0) + int(record.get("b") or 0)
            except (TypeError, ValueError):
                pass
            ua = str(record.get("u") or "")
            if not ua or BOTS.search(ua) or record.get("m") == "OPTIONS":
                return
            visitor = self._visitor(str(record.get("a") or ""), ua)
            seen = self.today.setdefault(domain, set())
            if visitor not in seen:
                seen.add(visitor)
                key = domain.encode() + visitor.to_bytes(8, "little")
                if key in self.bloom_cur or key in self.bloom_prev:
                    self.returning[domain] = self.returning.get(domain, 0) + 1
            self.hour_sets.setdefault(domain, set()).add(visitor)

    def _roll(self, ts: float) -> None:
        hour = int(ts // 3600)
        if hour > self.hour:  # tylko do przodu — spóźnione pakiety liczymy do bieżącego kubełka
            self._write_hour()
            self.hour, self.hour_sets, self.hour_requests = hour, {}, {}
        day = _day(ts)
        if day > self.day:
            self._write_day()
            for domain, visitors in self.today.items():
                prefix = domain.encode()
                for visitor in visitors:
                    self.bloom_cur.add(prefix + visitor.to_bytes(8, "little"))
            if ts - self.bloom_cur.created > BLOOM_ROTATE_DAYS * 86400:
                self.bloom_prev, self.bloom_cur = self.bloom_cur, Bloom(created=ts)
            self._save_blooms()
            self.day, self.today, self.returning, self.requests, self.bytes = day, {}, {}, {}, {}
            self.db.execute("DELETE FROM daily WHERE day < ?", [_day(ts - DAILY_KEEP_DAYS * 86400)])
            self.db.execute("DELETE FROM hourly WHERE hour < ?", [hour - HOURLY_KEEP_HOURS])

    def _write_day(self) -> None:
        for domain in set(self.requests) | set(self.today):
            self.db.execute(
                "INSERT INTO daily (domain, day, requests, uniques, returners, bytes) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(domain, day) DO UPDATE SET requests = excluded.requests, uniques = excluded.uniques, "
                "returners = excluded.returners, bytes = excluded.bytes",
                [domain, self.day, self.requests.get(domain, 0), len(self.today.get(domain, ())), self.returning.get(domain, 0),
                 self.bytes.get(domain, 0)],
            )  # fmt: skip

    def _write_hour(self) -> None:
        for domain in set(self.hour_requests) | set(self.hour_sets):
            self.db.execute(
                "INSERT INTO hourly (domain, hour, requests, uniques) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(domain, hour) DO UPDATE SET requests = excluded.requests, uniques = excluded.uniques",
                [domain, self.hour, self.hour_requests.get(domain, 0), len(self.hour_sets.get(domain, ()))],
            )

    def flush(self) -> None:
        with self._lock:
            self._roll(time.time())
            self._write_day()
            self._write_hour()
        self.save_today()

    # --- odczyt -------------------------------------------------------------------------

    def daily(self, days: int = 30, domains: list[str] | None = None) -> dict[str, list[dict]]:
        self.flush()
        since = _day(time.time() - (days - 1) * 86400)
        sql = "SELECT domain, day, requests, uniques, returners, bytes FROM daily WHERE day >= ?"
        params: list[Any] = [since]
        if domains:
            sql += f" AND domain IN ({','.join('?' for _ in domains)})"
            params += domains
        out: dict[str, list[dict]] = {}
        with self._lock:
            rows = self.db.execute(sql + " ORDER BY day", params).fetchall()
        for domain, day, requests, uniques, returning, size in rows:
            out.setdefault(domain, []).append(
                {"day": day, "requests": requests, "uniques": uniques, "returning": returning, "bytes": size}
            )
        return out

    def hourly(self, hours: int = 24, domains: list[str] | None = None) -> dict[str, list[dict]]:
        self.flush()
        since = int(time.time() // 3600) - hours + 1
        sql = "SELECT domain, hour, requests, uniques FROM hourly WHERE hour >= ?"
        params: list[Any] = [since]
        if domains:
            sql += f" AND domain IN ({','.join('?' for _ in domains)})"
            params += domains
        out: dict[str, list[dict]] = {}
        with self._lock:
            rows = self.db.execute(sql + " ORDER BY hour", params).fetchall()
        for domain, hour, requests, uniques in rows:
            out.setdefault(domain, []).append({"hour": hour, "requests": requests, "uniques": uniques})
        return out


class SyslogReceiver(asyncio.DatagramProtocol):
    """Odbiera linie `<pri>… deployra: {json}` od nginx (statystyki odwiedzin + ruch per IP)."""

    def __init__(self, analytics: Analytics, ipwatch: Any = None) -> None:
        self.analytics = analytics
        self.ipwatch = ipwatch

    def datagram_received(self, data: bytes, addr: Any) -> None:
        text = data.decode("utf-8", errors="replace")
        start = text.find("{")
        if start < 0:
            return
        try:
            record = json.loads(text[start:])
            self.analytics.ingest(record)
            if self.ipwatch is not None:
                self.ipwatch.ingest(record)
        except (ValueError, TypeError, AttributeError):
            pass


async def run(analytics: Analytics, host: str, port: int, flush_every: float = 60.0, ipwatch: Any = None) -> None:
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: SyslogReceiver(analytics, ipwatch), local_addr=(host, port))
    logger.info(tr("gw.log.analytics_listening", address=f"{host}:{port}"))

    def flush() -> None:
        analytics.flush()
        if ipwatch is not None:
            ipwatch.flush()

    try:
        while True:
            await asyncio.sleep(flush_every)
            await asyncio.to_thread(flush)
    finally:
        transport.close()
        await asyncio.to_thread(flush)

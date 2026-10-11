"""Kraj i operator (ISP/ASN) adresu IP — darmowe bazy DB-IP Lite (CC BY 4.0, „IP Geolocation by DB-IP”).

Bramka sama pobiera bazy co miesiąc (mmdb, ~10 MB każda) i czyta je przez mmap, więc w RAM-ie
siedzą tylko strony faktycznie używane przy wyszukiwaniu. Adresy prywatne pomijamy.
"""

from __future__ import annotations

import gzip
import ipaddress
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from deployra_common.i18n import tr

URL = "https://download.db-ip.com/free/dbip-{kind}-lite-{month}.mmdb.gz"
KINDS = ("country", "asn")
MAX_AGE = 35 * 86400
CACHE = 4096


class GeoIP:
    def __init__(self, data_dir: Path, enabled: bool = True) -> None:
        self.dir = data_dir / "geoip"
        self.enabled = enabled
        self._readers: dict[str, Any] = {}
        self._cache: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()

    def path(self, kind: str) -> Path:
        return self.dir / f"dbip-{kind}-lite.mmdb"

    def _reader(self, kind: str) -> Any:
        if kind not in self._readers:
            reader = None
            if self.enabled and self.path(kind).is_file():
                try:
                    import maxminddb  # leniwie: bez baz nie ładujemy biblioteki

                    reader = maxminddb.open_database(str(self.path(kind)), maxminddb.MODE_MMAP)
                except Exception as exc:  # noqa: BLE001 — uszkodzony plik nie może wywrócić bramki
                    logger.warning(tr("gw.log.geoip_broken", kind=kind, error=exc))
            self._readers[kind] = reader
        return self._readers[kind]

    def lookup(self, ip: str, lang: str = "en") -> dict[str, Any]:
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return {}
        if not address.is_global:
            return {"private": True}
        key = (ip, lang)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        out: dict[str, Any] = {}
        country = self._reader("country")
        if country is not None:
            record = country.get(ip) or {}
            info = record.get("country") or {}
            names = info.get("names") or {}
            if info.get("iso_code"):
                out["country"] = info["iso_code"]
                out["country_name"] = names.get(lang) or names.get("en") or info["iso_code"]
        asn = self._reader("asn")
        if asn is not None:
            record = asn.get(ip) or {}
            if record.get("autonomous_system_number"):
                out["asn"] = record["autonomous_system_number"]
                out["isp"] = record.get("autonomous_system_organization") or ""
        self._cache[key] = out
        if len(self._cache) > CACHE:
            self._cache.popitem(last=False)
        return out

    def status(self) -> dict[str, Any]:
        files = {kind: self.path(kind) for kind in KINDS}
        return {
            "enabled": self.enabled,
            **{kind: path.is_file() for kind, path in files.items()},
            "updated": max((path.stat().st_mtime for path in files.values() if path.is_file()), default=None),
        }

    def stale(self) -> list[str]:
        now = time.time()
        return [kind for kind in KINDS if not self.path(kind).is_file() or now - self.path(kind).stat().st_mtime > MAX_AGE]

    async def refresh(self, force: bool = False, transport: Any = None) -> list[str]:
        """Pobiera brakujące albo stare bazy (bieżący miesiąc, a gdy go jeszcze nie ma — poprzedni)."""
        if not self.enabled:
            return []
        import httpx  # leniwie — potrzebne raz w miesiącu

        kinds = list(KINDS) if force else self.stale()
        done = []
        today = datetime.now(timezone.utc)
        months = [today.strftime("%Y-%m"), (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")]
        self.dir.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=120, follow_redirects=True, transport=transport) as client:
            for kind in kinds:
                for month in months:
                    response = await client.get(URL.format(kind=kind, month=month))
                    if response.status_code == 404:
                        continue
                    response.raise_for_status()
                    tmp = self.path(kind).with_suffix(".tmp")
                    tmp.write_bytes(gzip.decompress(response.content))
                    self._close(kind)
                    tmp.replace(self.path(kind))
                    done.append(kind)
                    logger.info(tr("gw.log.geoip_updated", kind=kind, month=month))
                    break
        self._cache.clear()
        return done

    def _close(self, kind: str) -> None:
        reader = self._readers.pop(kind, None)
        if reader is not None:
            try:
                reader.close()
            except Exception:  # noqa: BLE001
                pass

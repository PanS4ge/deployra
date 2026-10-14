"""Próbkowanie zasobów hosta w tle (bez blokującego `cpu_percent(interval=1)`)."""

from __future__ import annotations

import threading
import time
from typing import Any

import psutil


def format_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f}{unit}" if unit in ("B", "KB") else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


class VitalsSampler:
    def __init__(self, interval: float = 4.0, disk_path: str = "/") -> None:
        self.interval = interval
        self.disk_path = disk_path
        self._latest: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_requested: float = time.time()
        psutil.cpu_percent(None)
        psutil.cpu_percent(None, percpu=True)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="vitals", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.sample()
            # Tryb oszczędzania energii dla małych urządzeń (np. Raspberry Pi Zero, VPS 512MB):
            # gdy nikt nie odpytuje o metryki przez >60s, zmniejszamy częstotliwość próbkowania
            idle = (time.time() - self._last_requested) > 60.0
            delay = (self.interval * 2.5) if idle else self.interval
            self._stop.wait(delay)

    def sample(self) -> dict[str, Any]:
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        try:
            disk = psutil.disk_usage(self.disk_path)
            disk_info = {"disk": disk.percent, "disk_total": disk.total, "disk_used": disk.used}
        except OSError:
            disk_info = {"disk": None, "disk_total": None, "disk_used": None}
        try:
            load = list(psutil.getloadavg())
        except (AttributeError, OSError):
            load = None
        net = psutil.net_io_counters()
        data = {
            "cpu": psutil.cpu_percent(None),
            "cores": psutil.cpu_percent(None, percpu=True),
            "ram": mem.percent,
            "ram_total": mem.total,
            "ram_used": mem.total - mem.available,
            "swap": swap.percent,
            "load": load,
            "uptime": int(time.time() - psutil.boot_time()),
            "net_rx": net.bytes_recv,
            "net_tx": net.bytes_sent,
            "ts": time.time(),
            **disk_info,
        }
        with self._lock:
            self._latest = data
        return data

    def latest(self) -> dict[str, Any]:
        self._last_requested = time.time()
        with self._lock:
            if self._latest:
                return dict(self._latest)
        return self.sample()

    @staticmethod
    def static_info() -> dict[str, Any]:
        mem = psutil.virtual_memory()
        return {
            "cpu_physical": psutil.cpu_count(logical=False),
            "cpu_logical": psutil.cpu_count(logical=True),
            "ram_total": mem.total,
            "ram_human": format_bytes(mem.total),
        }

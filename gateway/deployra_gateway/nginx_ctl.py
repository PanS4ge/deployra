"""Atomowa podmiana konfiguracji nginx z testem i rollbackiem.

nginx działa w osobnym, oficjalnym kontenerze. Rozmawiamy z nim przez wspólny
wolumen: zapisujemy `ctl/<id>.request`, a skrypt `deployra-reloader.sh`
(podmontowany do /docker-entrypoint.d/) wykonuje `nginx -t`, przy sukcesie
`nginx -s reload`, i odpisuje `ctl/<id>.result`. Nie potrzebujemy docker.sock.
"""

from __future__ import annotations

import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from deployra_common.i18n import tr

MANAGED = ("http", "stream", "access")
BASE_FILES = ("nginx.conf", "deployra-reloader.sh")


def install_base(nginx_dir: Path, bundle: Path) -> bool:
    """Kopiuje nginx.conf i skrypt przeładowania z obrazu bramki do wspólnego wolumenu (`base/`).

    Kontener nginx czyta je stamtąd, więc nowa wersja bramki przynosi swoją konfigurację
    bez montowania plików z repozytorium. Zwraca True, gdy coś się zmieniło.
    """
    target = nginx_dir / "base"
    target.mkdir(parents=True, exist_ok=True)
    changed = False
    for name in BASE_FILES:
        source = bundle / name
        if not source.is_file():
            raise FileNotFoundError(tr("gw.bundle_missing", path=source))
        content = source.read_bytes().replace(b"\r\n", b"\n")
        current = target / name
        if current.exists() and current.read_bytes() == content:
            continue
        tmp = target / f".{name}.tmp"
        tmp.write_bytes(content)
        tmp.replace(current)
        changed = True
    return changed


@dataclass
class ApplyResult:
    ok: bool
    status: str  # applied | rejected | pending | written
    message: str = ""


class NginxControl:
    def __init__(self, nginx_dir: Path, reload: bool = True, timeout: float = 20.0) -> None:
        self.dir = nginx_dir
        self.reload = reload
        self.timeout = timeout
        self._lock = threading.Lock()

    @property
    def ctl(self) -> Path:
        return self.dir / "ctl"

    def request_reload(self) -> ApplyResult:
        """Zleca `nginx -t` + reload i czeka na wynik."""
        self.ctl.mkdir(parents=True, exist_ok=True)
        req_id = uuid.uuid4().hex[:12]
        request, result = self.ctl / f"{req_id}.request", self.ctl / f"{req_id}.result"
        tmp = self.ctl / f".{req_id}.tmp"
        tmp.write_text(str(time.time()), encoding="utf-8")
        tmp.rename(request)
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if result.exists():
                text = result.read_text(encoding="utf-8", errors="replace")
                result.unlink(missing_ok=True)
                status, _, details = text.partition("\n")
                if status.strip() == "ok":
                    return ApplyResult(True, "applied")
                return ApplyResult(False, "rejected", details.strip() or status.strip())
            time.sleep(0.2)
        request.unlink(missing_ok=True)
        return ApplyResult(False, "pending", tr("gw.nginx_pending"))

    def apply(self, files: dict[str, str]) -> ApplyResult:
        """Podmienia katalogi http/stream/access na nowe; przy odrzuceniu przywraca poprzednie."""
        for rel in files:
            parts = Path(rel).parts
            if len(parts) != 2 or parts[0] not in MANAGED or parts[1].startswith("."):
                raise ValueError(tr("gw.bad_conf_path", path=rel))
        with self._lock:
            stamp = uuid.uuid4().hex[:8]
            staging = self.dir / f".staging-{stamp}"
            backup = self.dir / f".backup-{stamp}"
            for sub in MANAGED:
                (staging / sub).mkdir(parents=True, exist_ok=True)
            for rel, content in files.items():
                (staging / rel).write_text(content, encoding="utf-8")
            backup.mkdir()
            for sub in MANAGED:
                current = self.dir / sub
                if current.exists():
                    current.rename(backup / sub)
                (staging / sub).rename(current)
            shutil.rmtree(staging, ignore_errors=True)

            if not self.reload:
                shutil.rmtree(backup, ignore_errors=True)
                return ApplyResult(True, "written")
            result = self.request_reload()
            if result.status == "rejected":
                for sub in MANAGED:
                    shutil.rmtree(self.dir / sub, ignore_errors=True)
                    if (backup / sub).exists():
                        (backup / sub).rename(self.dir / sub)
                    else:
                        (self.dir / sub).mkdir()
            shutil.rmtree(backup, ignore_errors=True)
            return result

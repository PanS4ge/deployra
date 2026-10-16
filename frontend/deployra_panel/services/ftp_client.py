"""Moduł klienta FTP/FTPS do zarządzania plikami na serwerach Minecraft (hostingach współdzielonych)."""

from __future__ import annotations

import ftplib
import io
import posixpath
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class FtpFileItem:
    name: str
    is_dir: bool
    size: int
    modified: str
    path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FtpClientService:
    """Klient FTP z obsługą plain FTP oraz FTPS (TLS)."""

    def __init__(
        self,
        host: str,
        port: int = 21,
        username: str = "",
        password: str = "",
        use_tls: bool = False,
        timeout: float = 8.0,
    ):
        self.host = host.strip()
        self.port = int(port) if port else 21
        self.username = username.strip()
        self.password = password
        self.use_tls = bool(use_tls)
        self.timeout = float(timeout)

    def _connect(self) -> ftplib.FTP:
        if not self.host:
            raise ValueError("Adres hosta FTP nie został podany.")

        if self.use_tls:
            ftp = ftplib.FTP_TLS(timeout=self.timeout)
            ftp.connect(self.host, self.port)
            ftp.login(self.username or "anonymous", self.password or "")
            ftp.prot_p()
        else:
            ftp = ftplib.FTP(timeout=self.timeout)
            ftp.connect(self.host, self.port)
            ftp.login(self.username or "anonymous", self.password or "")

        ftp.set_pasv(True)
        ftp.encoding = "utf-8"
        return ftp

    def test_connection(self) -> tuple[bool, str]:
        """Testuje połączenie i logowanie do serwera FTP."""
        try:
            with self._connect() as ftp:
                welcome = ftp.getwelcome()
                return True, welcome or "Połączono pomyślnie z serwerem FTP."
        except Exception as e:
            return False, f"Błąd FTP: {e}"

    def list_dir(self, remote_path: str = "/") -> list[FtpFileItem]:
        """Zwraca listę plików i katalogów w podanej ścieżce."""
        remote_path = posixpath.normpath(remote_path or "/")
        if not remote_path.startswith("/"):
            remote_path = "/" + remote_path

        items: list[FtpFileItem] = []
        with self._connect() as ftp:
            ftp.cwd(remote_path)
            lines: list[str] = []
            try:
                ftp.retrlines("LIST", lines.append)
            except Exception:
                try:
                    for name in ftp.nlst():
                        clean = posixpath.basename(name)
                        if clean in (".", ".."):
                            continue
                        items.append(
                            FtpFileItem(
                                name=clean,
                                is_dir=False,
                                size=0,
                                modified="",
                                path=posixpath.join(remote_path, clean),
                            )
                        )
                    return items
                except Exception as e:
                    raise RuntimeError(f"Nie udało się wylistować plików: {e}")

            for line in lines:
                item = self._parse_line(line, remote_path)
                if item and item.name not in (".", ".."):
                    items.append(item)

        items.sort(key=lambda x: (not x.is_dir, x.name.lower()))
        return items

    def read_text(self, remote_path: str, max_bytes: int = 5 * 1024 * 1024) -> str:
        """Pobiera zawartość pliku tekstowego."""
        buf = io.BytesIO()
        with self._connect() as ftp:
            ftp.retrbinary(f"RETR {remote_path}", buf.write)

        data = buf.getvalue()
        if len(data) > max_bytes:
            raise ValueError(f"Plik jest zbyt duży ({len(data)} bajtów, max {max_bytes}).")
        return data.decode("utf-8", errors="replace")

    def write_text(self, remote_path: str, content: str) -> None:
        """Zapisuje treść tekstową do pliku na serwerze FTP."""
        buf = io.BytesIO(content.encode("utf-8"))
        with self._connect() as ftp:
            ftp.storbinary(f"STOR {remote_path}", buf)

    def upload_bytes(self, remote_path: str, data: bytes) -> None:
        """Wgrywa binarne dane pliku na serwer FTP."""
        buf = io.BytesIO(data)
        with self._connect() as ftp:
            ftp.storbinary(f"STOR {remote_path}", buf)

    def download_bytes(self, remote_path: str) -> bytes:
        """Pobiera surowe bajty pliku z serwera FTP."""
        buf = io.BytesIO()
        with self._connect() as ftp:
            ftp.retrbinary(f"RETR {remote_path}", buf.write)
        return buf.getvalue()

    def delete(self, remote_path: str, is_dir: bool = False) -> None:
        """Usuwa plik lub pusty katalog."""
        with self._connect() as ftp:
            if is_dir:
                ftp.rmd(remote_path)
            else:
                ftp.delete(remote_path)

    def mkdir(self, remote_path: str) -> None:
        """Tworzy katalog."""
        with self._connect() as ftp:
            ftp.mkd(remote_path)

    @staticmethod
    def _parse_line(line: str, current_dir: str) -> FtpFileItem | None:
        parts = line.split()
        if not parts:
            return None

        # Format unixowy (standard ftpd / vsftpd / proftpd):
        # drwxr-xr-x 2 user group 4096 Oct 16 12:00 plugins
        is_dir = parts[0].startswith("d")
        size = 0
        date_str = ""

        try:
            size = int(parts[4]) if not is_dir else 0
        except (IndexError, ValueError):
            pass

        if len(parts) >= 9:
            name = " ".join(parts[8:])
            date_str = " ".join(parts[5:8])
        else:
            name = parts[-1]

        # Format IIS / Windows FTP:
        # 04-16-20 01:23PM <DIR> plugins
        if "<DIR>" in parts:
            is_dir = True
            name = parts[-1]
            size = 0

        clean_name = posixpath.basename(name)
        return FtpFileItem(
            name=clean_name,
            is_dir=is_dir,
            size=size,
            modified=date_str,
            path=posixpath.join(current_dir, clean_name),
        )

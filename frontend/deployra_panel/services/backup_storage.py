"""Wielocelowy system kopii zapasowych (Lokalny dysk/NAS, S3/MinIO, FTP, SFTP)."""

from __future__ import annotations

import ftplib
import hashlib
import hmac
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ..context import Panel
from ..db import now

BACKUP_TABLES = (
    "settings",
    "access_lists",
    "certificates",
    "sites",
    "streams",
    "monitors",
    "apps",
    "nodes",
    "dns_records",
)


def create_backup_payload(panel: Panel, secrets_included: bool = True) -> tuple[str, bytes]:
    """Generuje plik kopii zapasowej JSON i zwraca (filename, bytes)."""
    data = {"deployra": 1, "version": 2, "exported_at": now(), "tables": {}}
    for table in BACKUP_TABLES:
        try:
            rows = panel.db.fetchall(f"SELECT * FROM {table}")
            if table == "settings" and not secrets_included:
                rows = [r for r in rows if r.get("skey") not in ("cloudflare_token", "backup_config")]
            data["tables"][table] = rows
        except Exception:
            pass
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")[:22]
    filename = f"deployra-backup-{stamp}.json"
    content = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    return filename, content


class LocalDriver:
    def __init__(self, path: str, retention: int = 10):
        self.path = Path(path)
        self.retention = max(1, int(retention or 10))

    def test(self) -> str:
        self.path.mkdir(parents=True, exist_ok=True)
        test_file = self.path / ".deployra_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
        return f"Katalog lokalny {self.path} jest dostępny i zapisywalny"

    def upload(self, filename: str, data: bytes) -> str:
        self.path.mkdir(parents=True, exist_ok=True)
        target = self.path / filename
        target.write_bytes(data)
        self._prune()
        return f"Zapisano w {target}"

    def _prune(self) -> None:
        try:
            files = sorted(self.path.glob("deployra-backup-*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
            for old in files[self.retention:]:
                try:
                    old.unlink()
                except Exception:
                    pass
        except Exception:
            pass


class FtpDriver:
    def __init__(self, host: str, port: int = 21, user: str = "anonymous", password: str = "", path: str = "/", tls: bool = False, retention: int = 10):
        self.host = host.strip()
        self.port = int(port or 21)
        self.user = user.strip() or "anonymous"
        self.password = password or ""
        self.path = path.strip() or "/"
        self.tls = bool(tls)
        self.retention = max(1, int(retention or 10))

    def _connect(self) -> ftplib.FTP:
        cls = ftplib.FTP_TLS if self.tls else ftplib.FTP
        ftp = cls()
        ftp.connect(self.host, self.port, timeout=15)
        ftp.login(self.user, self.password)
        if self.tls and isinstance(ftp, ftplib.FTP_TLS):
            ftp.prot_p()
        if self.path and self.path != "/":
            for part in self.path.strip("/").split("/"):
                if not part:
                    continue
                try:
                    ftp.cwd(part)
                except ftplib.error_perm:
                    try:
                        ftp.mkd(part)
                        ftp.cwd(part)
                    except Exception:
                        pass
        return ftp

    def test(self) -> str:
        with self._connect() as ftp:
            pwd = ftp.pwd()
            return f"Połączono z serwerem FTP {self.host}:{self.port} (katalog: {pwd})"

    def upload(self, filename: str, data: bytes) -> str:
        with self._connect() as ftp:
            ftp.storbinary(f"STOR {filename}", io.BytesIO(data))
            try:
                names = ftp.nlst()
                backups = sorted([n for n in names if n.startswith("deployra-backup-") and n.endswith(".json")], reverse=True)
                for old in backups[self.retention:]:
                    try:
                        ftp.delete(old)
                    except Exception:
                        pass
            except Exception:
                pass
            return f"Przesłano {filename} na serwer FTP {self.host}"


class SftpDriver:
    def __init__(self, host: str, port: int = 22, user: str = "root", password: str = "", path: str = "/backups", retention: int = 10):
        self.host = host.strip()
        self.port = int(port or 22)
        self.user = user.strip() or "root"
        self.password = password or ""
        self.path = path.strip() or "/backups"
        self.retention = max(1, int(retention or 10))

    def _connect(self) -> tuple[Any, Any]:
        import paramiko

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(self.host, port=self.port, username=self.user, password=self.password or None, timeout=15)
        sftp = ssh.open_sftp()
        try:
            sftp.chdir(self.path)
        except IOError:
            parts = self.path.strip("/").split("/")
            curr = ""
            for p in parts:
                curr += "/" + p
                try:
                    sftp.mkdir(curr)
                except IOError:
                    pass
            sftp.chdir(self.path)
        return ssh, sftp

    def test(self) -> str:
        ssh, sftp = self._connect()
        try:
            pwd = sftp.getcwd() or self.path
            return f"Połączono z serwerem SFTP {self.host}:{self.port} (katalog: {pwd})"
        finally:
            sftp.close()
            ssh.close()

    def upload(self, filename: str, data: bytes) -> str:
        ssh, sftp = self._connect()
        try:
            with sftp.file(filename, "wb") as f:
                f.write(data)
            try:
                files = sftp.listdir()
                backups = sorted([n for n in files if n.startswith("deployra-backup-") and n.endswith(".json")], reverse=True)
                for old in backups[self.retention:]:
                    try:
                        sftp.remove(old)
                    except Exception:
                        pass
            except Exception:
                pass
            return f"Przesłano {filename} na serwer SFTP {self.host}"
        finally:
            sftp.close()
            ssh.close()


def _sigv4_headers(method: str, url: str, body: bytes, access_key: str, secret_key: str, region: str = "us-east-1", service: str = "s3") -> dict[str, str]:
    parsed = urlparse(url)
    host = parsed.netloc
    path = parsed.path or "/"
    now_dt = datetime.now(timezone.utc)
    amz_date = now_dt.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now_dt.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()

    canonical_headers = f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = f"{method}\n{path}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"

    algorithm = "AWS4-HMAC-SHA256"
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = f"{algorithm}\n{amz_date}\n{credential_scope}\n{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"

    def _sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_date = _sign(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    k_signing = _sign(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    auth_header = f"{algorithm} Credential={access_key}/{credential_scope}, SignedHeaders={signed_headers}, Signature={signature}"
    return {
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
        "Authorization": auth_header,
    }


class S3Driver:
    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str, region: str = "us-east-1", prefix: str = "", retention: int = 10):
        self.endpoint = (endpoint.strip() if endpoint else "https://s3.amazonaws.com").rstrip("/")
        if not self.endpoint.startswith("http://") and not self.endpoint.startswith("https://"):
            self.endpoint = f"https://{self.endpoint}"
        self.bucket = bucket.strip()
        self.access_key = access_key.strip()
        self.secret_key = secret_key.strip()
        self.region = region.strip() or "us-east-1"
        self.prefix = prefix.strip().strip("/")
        self.retention = max(1, int(retention or 10))

    def _url(self, key: str = "") -> str:
        key = key.lstrip("/")
        if key:
            return f"{self.endpoint}/{self.bucket}/{key}"
        return f"{self.endpoint}/{self.bucket}"

    def test(self) -> str:
        url = self._url()
        headers = _sigv4_headers("HEAD", url, b"", self.access_key, self.secret_key, self.region)
        with httpx.Client(timeout=10.0) as client:
            resp = client.head(url, headers=headers)
            if resp.status_code in (200, 404):  # 200 bucket exists, 404 key exists
                return f"Połączono z bucketem S3 '{self.bucket}'"
            if resp.status_code == 403:
                raise ValueError(f"Odmowa dostępu do bucketu S3 '{self.bucket}' (HTTP 403)")
            raise ValueError(f"Błąd S3: status {resp.status_code}")

    def upload(self, filename: str, data: bytes) -> str:
        key = f"{self.prefix}/{filename}" if self.prefix else filename
        url = self._url(key)
        headers = _sigv4_headers("PUT", url, data, self.access_key, self.secret_key, self.region)
        headers["Content-Type"] = "application/json"
        with httpx.Client(timeout=30.0) as client:
            resp = client.put(url, headers=headers, content=data)
            if resp.status_code not in (200, 204):
                raise ValueError(f"Błąd wysyłania do S3: HTTP {resp.status_code} {resp.text[:200]}")
            return f"Wysłano do S3 '{self.bucket}/{key}'"


def get_driver(panel: Panel, config: dict | None = None) -> Any:
    conf = config or panel.db.setting("backup_config") or {}
    target = conf.get("target") or "local"
    retention = int(conf.get("retention") or 10)

    if target == "local":
        path = conf.get("local_path") or "./backups"
        return LocalDriver(path=path, retention=retention)
    elif target == "ftp":
        pwd = conf.get("ftp_password") or ""
        if isinstance(pwd, dict) and "sealed" in pwd:
            pwd = panel.sealer.unseal(pwd)
        return FtpDriver(
            host=conf.get("ftp_host") or "127.0.0.1",
            port=conf.get("ftp_port") or 21,
            user=conf.get("ftp_user") or "anonymous",
            password=pwd,
            path=conf.get("ftp_path") or "/",
            tls=bool(conf.get("ftp_tls")),
            retention=retention,
        )
    elif target == "sftp":
        pwd = conf.get("sftp_password") or ""
        if isinstance(pwd, dict) and "sealed" in pwd:
            pwd = panel.sealer.unseal(pwd)
        return SftpDriver(
            host=conf.get("sftp_host") or "127.0.0.1",
            port=conf.get("sftp_port") or 22,
            user=conf.get("sftp_user") or "root",
            password=pwd,
            path=conf.get("sftp_path") or "/backups",
            retention=retention,
        )
    elif target == "s3":
        secret = conf.get("s3_secret_key") or ""
        if isinstance(secret, dict) and "sealed" in secret:
            secret = panel.sealer.unseal(secret)
        return S3Driver(
            endpoint=conf.get("s3_endpoint") or "https://s3.amazonaws.com",
            bucket=conf.get("s3_bucket") or "backups",
            access_key=conf.get("s3_access_key") or "",
            secret_key=secret,
            region=conf.get("s3_region") or "us-east-1",
            prefix=conf.get("s3_prefix") or "",
            retention=retention,
        )
    else:
        raise ValueError(f"Nieznany cel kopii: {target}")


def run_backup(panel: Panel, config: dict | None = None) -> dict[str, Any]:
    conf = dict(config or panel.db.setting("backup_config") or {})
    driver = get_driver(panel, conf)
    secrets_included = conf.get("secrets_included", True)
    filename, data = create_backup_payload(panel, secrets_included=secrets_included)

    try:
        msg = driver.upload(filename, data)
        conf.update(
            last_run=now(),
            last_status="ok",
            last_message=msg,
            last_filename=filename,
            last_size=len(data),
        )
        panel.db.set_setting("backup_config", conf)
        return {"ok": True, "message": msg, "filename": filename, "size": len(data)}
    except Exception as exc:
        err_msg = str(exc)
        conf.update(
            last_run=now(),
            last_status="error",
            last_message=err_msg,
        )
        panel.db.set_setting("backup_config", conf)
        return {"ok": False, "error": err_msg}

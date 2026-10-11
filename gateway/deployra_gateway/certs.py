"""Certyfikaty: lego uruchamiane na żądanie (HTTP-01 webroot lub Cloudflare DNS-01)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography import x509

from deployra_common.i18n import tr
from deployra_common.jobs import Job, JobFailed
from deployra_common.schemas import CertRequest, CertUpload

LE_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"
RENEW_DAYS = 30


def cert_dir(certs_dir: Path, cert_id: int) -> Path:
    return certs_dir / str(int(cert_id))


def is_ready(certs_dir: Path, cert_id: int) -> bool:
    d = cert_dir(certs_dir, cert_id)
    return (d / "fullchain.pem").is_file() and (d / "privkey.pem").is_file()


def read_info(certs_dir: Path, cert_id: int) -> dict[str, Any] | None:
    path = cert_dir(certs_dir, cert_id) / "fullchain.pem"
    if not path.is_file():
        return None
    cert = x509.load_pem_x509_certificates(path.read_bytes())[0]
    try:
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        names = []
    not_after = cert.not_valid_after_utc
    return {
        "id": cert_id,
        "domains": names,
        "issuer": cert.issuer.rfc4514_string(),
        "not_after": not_after.isoformat(),
        "days_left": (not_after - datetime.now(timezone.utc)).days,
    }


def _install(certs_dir: Path, cert_id: int, fullchain: bytes, privkey: bytes) -> None:
    target = cert_dir(certs_dir, cert_id)
    target.mkdir(parents=True, exist_ok=True)
    for name, data, mode in (("fullchain.pem", fullchain, 0o644), ("privkey.pem", privkey, 0o600)):
        tmp = target / f".{name}.tmp"
        tmp.write_bytes(data)
        os.chmod(tmp, mode)
        tmp.replace(target / name)


def install_upload(certs_dir: Path, upload: CertUpload) -> dict:
    """Własny certyfikat (np. zaimportowany z NPM). Sprawdza, czy klucz pasuje do certyfikatu."""
    from cryptography.hazmat.primitives import serialization

    try:
        chain = x509.load_pem_x509_certificates(upload.fullchain.encode())
        key = serialization.load_pem_private_key(upload.privkey.encode(), password=None)
    except ValueError as exc:
        raise ValueError(tr("gw.bad_pem", error=exc)) from exc
    spki = serialization.PublicFormat.SubjectPublicKeyInfo
    if chain[0].public_key().public_bytes(serialization.Encoding.PEM, spki) != key.public_key().public_bytes(
        serialization.Encoding.PEM, spki
    ):
        raise ValueError(tr("gw.key_mismatch"))
    _install(certs_dir, upload.id, upload.fullchain.encode(), upload.privkey.encode())
    return read_info(certs_dir, upload.id) or {}


def lego_command(lego: str, data_dir: Path, acme_dir: Path, req: CertRequest, action: str) -> tuple[list[str], dict[str, str]]:
    cmd = [lego, "--accept-tos", "--email", req.email, "--path", str(data_dir / "lego")]
    if req.staging:
        cmd += ["--server", LE_STAGING]
    for domain in req.domains:
        cmd += ["--domains", domain]
    env: dict[str, str] = {}
    if req.challenge == "dns-cloudflare":
        cmd += ["--dns", "cloudflare", "--dns.resolvers", "1.1.1.1:53"]
        env["CF_DNS_API_TOKEN"] = req.cloudflare_token or ""
    else:
        cmd += ["--http", "--http.webroot", str(acme_dir)]
    cmd.append(action)
    if action == "renew":
        cmd += ["--days", str(RENEW_DAYS)]  # lego samo odczeka losowo do 8 min (prośba Let's Encrypt)
    return cmd, env


def _crt_key(data_dir: Path, first_domain: str) -> tuple[Path, Path]:
    stem = data_dir / "lego" / "certificates" / first_domain.replace("*", "_")
    return Path(f"{stem}.crt"), Path(f"{stem}.key")


def run_lego(job: Job, cfg: Any, req: CertRequest, action: str) -> bool:
    """Uruchamia lego; zwraca True, jeśli pliki certyfikatu się zmieniły."""
    if not shutil.which(cfg.lego_bin):
        raise JobFailed(tr("gw.no_lego"))
    cmd, env = lego_command(cfg.lego_bin, cfg.data_dir, cfg.acme_dir, req, action)
    crt, key = _crt_key(cfg.data_dir, req.domains[0])
    before = crt.stat().st_mtime if crt.exists() else None
    job.log("$ " + " ".join(cmd))  # token Cloudflare idzie w zmiennej środowiskowej, nie w poleceniu
    proc = subprocess.Popen(
        cmd, env={**os.environ, **env}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace"
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        job.log(line.rstrip("\n"))
    if proc.wait() != 0:
        raise JobFailed(tr("gw.lego_failed"))
    if not crt.exists() or not key.exists():
        raise JobFailed(tr("gw.lego_no_files"))
    changed = before is None or crt.stat().st_mtime != before
    if changed or not is_ready(cfg.certs_dir, req.id):
        _install(cfg.certs_dir, req.id, crt.read_bytes(), key.read_bytes())
    return changed


def remember(state: Any, req: CertRequest) -> None:
    state.mutate("certs", lambda certs: {**(certs or {}), str(req.id): json.loads(req.model_dump_json())})


def forget(state: Any, certs_dir: Path, cert_id: int) -> None:
    state.mutate("certs", lambda certs: {k: v for k, v in (certs or {}).items() if k != str(cert_id)})
    shutil.rmtree(cert_dir(certs_dir, cert_id), ignore_errors=True)

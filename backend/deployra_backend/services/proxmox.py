"""Zarządzanie kontenerami LXC i maszynami QEMU/KVM na węźle Proxmox VE.

Gdy kontener LXC nie ma Dockera, Deployra traktuje go jako osobną aplikację/środowisko.
Maszyny wirtualne QEMU/KVM są od razu traktowane jako aplikacje z podglądem konsoli noVNC.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_LXC_CGROUP = re.compile(r"/lxc(?:\.payload)?[./](\d+)(?:/|$)")
SOCKETS = ("run/docker.sock", "var/run/docker.sock")


def _read_kv_conf(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.is_file():
        return data
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return data
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            break  # snapshoty
        if ":" in line:
            k, v = line.split(":", 1)
            data[k.strip()] = v.strip()
    return data


def parse_lxc_conf(pve_dir: Path, vmid: int) -> dict[str, Any]:
    raw = _read_kv_conf(pve_dir / f"{vmid}.conf")
    ip = None
    net0 = raw.get("net0", "")
    for part in net0.split(","):
        if part.startswith("ip="):
            ip = part.split("=", 1)[1].split("/")[0].strip()
            if ip == "dhcp":
                ip = None
    return {
        "vmid": vmid,
        "name": raw.get("hostname", f"CT {vmid}"),
        "ostype": raw.get("ostype", "linux"),
        "cores": int(raw["cores"]) if raw.get("cores", "").isdigit() else 1,
        "memory_mb": int(raw["memory"]) if raw.get("memory", "").isdigit() else 512,
        "swap_mb": int(raw["swap"]) if raw.get("swap", "").isdigit() else 512,
        "disk": raw.get("rootfs", ""),
        "unprivileged": raw.get("unprivileged") == "1",
        "ip": ip,
    }


def parse_qemu_conf(pve_qemu_dir: Path, vmid: int) -> dict[str, Any]:
    raw = _read_kv_conf(pve_qemu_dir / f"{vmid}.conf")
    cores = int(raw["cores"]) if raw.get("cores", "").isdigit() else 1
    sockets = int(raw["sockets"]) if raw.get("sockets", "").isdigit() else 1
    return {
        "vmid": vmid,
        "name": raw.get("name", f"VM {vmid}"),
        "ostype": raw.get("ostype", "other"),
        "cores": cores * sockets,
        "memory_mb": int(raw["memory"]) if raw.get("memory", "").isdigit() else 1024,
        "disk": raw.get("bootdisk") or raw.get("scsi0") or raw.get("virtio0") or raw.get("sata0") or "",
        "ip": None,
    }


def discover_proxmox_entities(
    proc: Path = Path("/proc"),
    pve_lxc_dir: Path = Path("/etc/pve/lxc"),
    pve_qemu_dir: Path = Path("/etc/pve/qemu-server"),
    probe_socket: Any = Path.is_socket,
) -> list[dict[str, Any]]:
    """Zwraca listę wszystkich kontenerów LXC i maszyn QEMU na Proxmoxie."""
    # 1. Sprawdź działające PIDy i cgroupy dla LXC oraz cmdline dla QEMU
    lxc_pids: dict[int, int] = {}
    running_qemu: set[int] = set()
    try:
        entries = os.listdir(proc)
    except OSError:
        entries = []

    for entry in entries:
        if not entry.isdigit():
            continue
        pid_dir = proc / entry
        # LXC
        try:
            cgroup = (pid_dir / "cgroup").read_text()
            m = _LXC_CGROUP.search(cgroup)
            if m:
                vmid = int(m.group(1))
                pid = int(entry)
                if vmid not in lxc_pids or pid < lxc_pids[vmid]:
                    lxc_pids[vmid] = pid
        except OSError:
            pass

        # QEMU / KVM
        try:
            cmdline = (pid_dir / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="ignore")
            if "kvm" in cmdline or "qemu-system" in cmdline:
                m_qemu = re.search(r"-id\s+(\d+)", cmdline)
                if m_qemu:
                    running_qemu.add(int(m_qemu.group(1)))
        except OSError:
            pass

    entities: list[dict[str, Any]] = []

    # 2. Przetwórz kontenery LXC
    if pve_lxc_dir.is_dir():
        try:
            for conf in pve_lxc_dir.glob("*.conf"):
                stem = conf.stem
                if not stem.isdigit():
                    continue
                vmid = int(stem)
                meta = parse_lxc_conf(pve_lxc_dir, vmid)
                is_running = vmid in lxc_pids
                pid = lxc_pids.get(vmid)
                has_docker = False
                if is_running and pid:
                    for rel in SOCKETS:
                        sock = proc / str(pid) / "root" / rel
                        try:
                            if probe_socket(sock):
                                has_docker = True
                                break
                        except OSError:
                            continue
                entities.append({
                    "vmid": vmid,
                    "kind": "lxc",
                    "status": "running" if is_running else "stopped",
                    "has_docker": has_docker,
                    "pid": pid,
                    **meta,
                })
        except OSError:
            pass

    # 3. Przetwórz maszyny QEMU / KVM
    if pve_qemu_dir.is_dir():
        try:
            for conf in pve_qemu_dir.glob("*.conf"):
                stem = conf.stem
                if not stem.isdigit():
                    continue
                vmid = int(stem)
                meta = parse_qemu_conf(pve_qemu_dir, vmid)
                is_running = vmid in running_qemu
                entities.append({
                    "vmid": vmid,
                    "kind": "qemu",
                    "status": "running" if is_running else "stopped",
                    "has_docker": False,
                    "pid": None,
                    **meta,
                })
        except OSError:
            pass

    entities.sort(key=lambda e: (e["kind"], e["vmid"]))
    return entities


def execute_proxmox_action(kind: str, vmid: int, action: str) -> None:
    """Uruchamia akcję na kontenerze LXC (pct) lub maszynie wirtualnej QEMU (qm)."""
    if kind not in ("lxc", "qemu"):
        raise ValueError(f"Nieznany typ jednostki Proxmoxa: {kind}")
    if action not in ("start", "stop", "reboot", "shutdown"):
        raise ValueError(f"Nieobsługiwana akcja: {action}")

    cmd_tool = "pct" if kind == "lxc" else "qm"
    bin_path = shutil.which(cmd_tool)
    if not bin_path:
        raise RuntimeError(f"Narzędzie {cmd_tool} nie jest dostępne na tym węźle")

    cmd = [cmd_tool, action, str(vmid)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            err = (res.stderr or res.stdout or "").strip()
            raise RuntimeError(f"{cmd_tool} {action} #{vmid} nie powiodło się: {err}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Przekroczono limit czasu operacji {cmd_tool} {action} #{vmid}")

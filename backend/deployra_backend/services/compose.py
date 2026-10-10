"""Stacki docker-compose (przez `docker compose` CLI — Engine API nie ma compose)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from deployra_common.jobs import Job, JobFailed

_STACK_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
COMPOSE_FILE = "compose.yml"
OVERRIDE_FILE = "deployra.override.yml"
ENV_FILE = ".env"


def check_name(name: str) -> str:
    if not _STACK_NAME.match(name):
        raise ValueError("nazwa stacka: małe litery, cyfry, - i _")
    return name


def parse_compose(text: str, expose_service: str | None = None) -> dict:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"błędny YAML: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("services"), dict) or not data["services"]:
        raise ValueError("plik compose musi mieć sekcję services")
    if expose_service and expose_service not in data["services"]:
        raise ValueError(f"brak usługi {expose_service!r} w pliku compose")
    return data


def _env_line(key: str, value: str) -> str:
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
        raise ValueError(f"nieprawidłowa zmienna: {key!r}")
    if "\n" in value or "\r" in value:
        raise ValueError(f"zmienna {key} nie może zawierać nowych linii")
    if "'" not in value:
        return f"{key}='{value}'"
    return f'{key}="' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$") + '"'


def write_stack(
    stacks_dir: Path, name: str, compose: str, env: dict[str, str] | None = None, override: dict | None = None
) -> Path:
    check_name(name)
    parse_compose(compose)
    stack_dir = stacks_dir / name
    stack_dir.mkdir(parents=True, exist_ok=True)
    (stack_dir / COMPOSE_FILE).write_text(compose, encoding="utf-8")
    env_path = stack_dir / ENV_FILE
    if env:
        env_path.write_text("\n".join(_env_line(k, v) for k, v in env.items()) + "\n", encoding="utf-8")
        os.chmod(env_path, 0o600)
    elif env_path.exists():
        env_path.unlink()
    override_path = stack_dir / OVERRIDE_FILE
    if override:
        override_path.write_text(yaml.safe_dump(override, sort_keys=False), encoding="utf-8")
    elif override_path.exists():
        override_path.unlink()
    return stack_dir


def expose_override(service: str, host_port: int, container_port: int, slug: str) -> dict:
    return {
        "services": {
            service: {
                "ports": [f"{host_port}:{container_port}"],
                "labels": {"deployra.app": slug},
            }
        }
    }


def compose_cmd(stack_dir: Path, name: str, *args: str) -> list[str]:
    cmd = ["docker", "compose", "-p", name, "-f", str(stack_dir / COMPOSE_FILE)]
    if (stack_dir / OVERRIDE_FILE).exists():
        cmd += ["-f", str(stack_dir / OVERRIDE_FILE)]
    if (stack_dir / ENV_FILE).exists():
        cmd += ["--env-file", str(stack_dir / ENV_FILE)]
    return cmd + list(args)


def run_streaming(job: Job, cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None, shown: str | None = None) -> None:
    """Uruchamia polecenie i przepisuje wyjście do logu zadania."""
    job.log(f"$ {shown or ' '.join(cmd)}")
    full_env = {**os.environ, "COMPOSE_ANSI": "never", "COMPOSE_PROGRESS": "plain", "BUILDKIT_PROGRESS": "plain", **(env or {})}
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=full_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1
        )
    except FileNotFoundError as exc:
        raise JobFailed(f"brak programu {cmd[0]!r} w obrazie backendu") from exc
    assert proc.stdout is not None
    for line in proc.stdout:
        job.log(line.rstrip("\n"))
    code = proc.wait()
    if code != 0:
        raise JobFailed(f"polecenie zakończyło się kodem {code}")


def stacks_root(base: Path, endpoint: str) -> Path:
    """Stacki CT trzymamy osobno (na Proxmoxie nazwy mogą się powtarzać między CT)."""
    return base if endpoint == "local" else base / "_ep" / endpoint


def up(job: Job, stacks_dir: Path, name: str, pull: bool = False, env: dict[str, str] | None = None) -> None:
    stack_dir = stacks_dir / check_name(name)
    if pull:
        run_streaming(job, compose_cmd(stack_dir, name, "pull"), cwd=stack_dir, env=env)
    run_streaming(job, compose_cmd(stack_dir, name, "up", "-d", "--remove-orphans"), cwd=stack_dir, env=env)


def action(job: Job, stacks_dir: Path, name: str, verb: str, env: dict[str, str] | None = None) -> None:
    allowed = {"down": ["down"], "restart": ["restart"], "stop": ["stop"], "start": ["start"], "pull": ["pull"]}
    if verb not in allowed:
        raise ValueError(f"nieznana akcja: {verb}")
    stack_dir = stacks_dir / check_name(name)
    run_streaming(job, compose_cmd(stack_dir, name, *allowed[verb]), cwd=stack_dir, env=env)


def remove(job: Job, stacks_dir: Path, name: str, volumes: bool = False, env: dict[str, str] | None = None) -> None:
    stack_dir = stacks_dir / check_name(name)
    if (stack_dir / COMPOSE_FILE).exists():
        run_streaming(
            job, compose_cmd(stack_dir, name, "down", *(["-v"] if volumes else []), "--remove-orphans"), cwd=stack_dir, env=env
        )
    shutil.rmtree(stack_dir, ignore_errors=True)
    job.log(f"Usunięto stack {name}")


def read_stack(stacks_dir: Path, name: str) -> dict[str, Any]:
    stack_dir = stacks_dir / check_name(name)
    compose_path = stack_dir / COMPOSE_FILE
    if not compose_path.exists():
        raise FileNotFoundError(name)
    env: dict[str, str] = {}
    env_path = stack_dir / ENV_FILE
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]
                    if line.partition("=")[2].startswith('"'):
                        value = value.replace("$$", "$").replace('\\"', '"').replace("\\\\", "\\")
                env[key] = value
    return {"name": name, "compose": compose_path.read_text(encoding="utf-8"), "env": env}


def list_stacks(client: Any, stacks_dir: Path) -> list[dict]:
    stacks: dict[str, dict] = {}
    for path in sorted(stacks_dir.glob(f"*/{COMPOSE_FILE}")):
        stacks[path.parent.name] = {"name": path.parent.name, "managed": True, "containers": [], "running": 0}
    for c in client.api.containers(all=True):
        project = (c.get("Labels") or {}).get("com.docker.compose.project")
        if not project:
            continue
        entry = stacks.setdefault(project, {"name": project, "managed": False, "containers": [], "running": 0})
        entry["containers"].append(
            {
                "id": c["Id"],
                "name": (c.get("Names") or ["/?"])[0].lstrip("/"),
                "service": (c.get("Labels") or {}).get("com.docker.compose.service"),
                "state": c.get("State"),
                "status": c.get("Status"),
            }
        )
        if c.get("State") == "running":
            entry["running"] += 1
    return list(stacks.values())

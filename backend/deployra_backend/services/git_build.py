"""Deploy z Gita: klon/aktualizacja repozytorium i build obrazu (`docker build`, BuildKit)."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
from pathlib import Path

from deployra_common.i18n import tr
from deployra_common.jobs import Job, JobFailed

from .compose import run_streaming


def _auth_args(token: str | None) -> list[str]:
    if not token:
        return []
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.extraHeader=Authorization: Basic {basic}"]


def _ssh_env(work: Path, data_dir: Path, ssh_key: str | None) -> dict[str, str]:
    if not ssh_key:
        return {}
    key_path = work / "deploy_key"
    key_path.write_text(ssh_key.strip() + "\n", encoding="utf-8")
    os.chmod(key_path, 0o600)
    known = data_dir / "known_hosts"
    return {
        "GIT_SSH_COMMAND": f"ssh -i {key_path} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new "
        f"-o UserKnownHostsFile={known}"
    }


def _git(job: Job, args: list[str], cwd: Path | None, env: dict[str, str], token: str | None, shown: str) -> None:
    cmd = ["git", *_auth_args(token), *args]
    run_streaming(job, cmd, cwd=cwd, env={"GIT_TERMINAL_PROMPT": "0", **env}, shown=shown)


def checkout(job: Job, builds_dir: Path, data_dir: Path, slug: str, url: str, branch: str, token: str | None, ssh_key: str | None) -> tuple[Path, str]:
    """Zwraca (katalog repozytorium, sha). Klon płytki (--depth 1)."""
    work = builds_dir / slug
    work.mkdir(parents=True, exist_ok=True)
    repo = work / "repo"
    env = _ssh_env(work, data_dir, ssh_key)
    if (repo / ".git").exists():
        _git(job, ["fetch", "--depth", "1", "origin", branch], repo, env, token, f"git fetch origin {branch}")
        _git(job, ["checkout", "--force", "FETCH_HEAD"], repo, env, None, "git checkout FETCH_HEAD")
        _git(job, ["clean", "-fdx"], repo, env, None, "git clean -fdx")
    else:
        shutil.rmtree(repo, ignore_errors=True)
        _git(job, ["clone", "--depth", "1", "--branch", branch, url, str(repo)], None, env, token, f"git clone {_safe_url(url)} ({branch})")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=False).stdout.strip()
    if not sha:
        raise JobFailed(tr("node.git.no_commit"))
    job.log(f"Commit {sha[:12]}")
    return repo, sha


def _safe_url(url: str) -> str:
    if "@" in url and url.startswith("https://"):
        return "https://" + url.split("@", 1)[1]
    return url


def inside(repo: Path, relative: str) -> Path:
    path = (repo / relative).resolve()
    if path != repo.resolve() and repo.resolve() not in path.parents:
        raise JobFailed(tr("node.git.path_escape", path=repr(relative)))
    return path


def build(job: Job, repo: Path, dockerfile: str, context: str, tag: str, env: dict[str, str] | None = None) -> None:
    dockerfile_path = inside(repo, dockerfile)
    context_path = inside(repo, context)
    if not dockerfile_path.is_file():
        raise JobFailed(tr("node.git.no_dockerfile", file=dockerfile))
    run_streaming(
        job,
        ["docker", "build", "--progress=plain", "-t", tag, "-f", str(dockerfile_path), str(context_path)],
        env={"DOCKER_BUILDKIT": "1", **(env or {})},
    )

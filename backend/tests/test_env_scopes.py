"""Zakresy zmiennych na węźle: zmienne budowania trafiają do `docker build` i instalacji, nie do działającego kontenera."""

from deployra_backend.services import git_build


def deploy(api, spec):
    job = api.request("POST", "/api/apps/deploy", json=spec).json()["job"]
    return api.wait_job(job["id"])


def app_container(ctx, slug):
    return next(c for c in ctx.docker.api.containers_.values() if c["name"] == f"app-{slug}")


def test_runtime_container_skips_build_only_vars_but_install_gets_them(api, ctx):
    installs = []
    ctx.docker.api.on_oneoff = lambda c: installs.append(c["env"]) if c["labels"].get("deployra.system") == "install" else None
    spec = {
        "slug": "scoped", "source": {"kind": "runtime", "runtime": "python-flask"},
        "env": {"RT": "1", "BLD": "2", "BOTH": "3"}, "env_scopes": {"BLD": "build", "BOTH": "both"},
    }  # fmt: skip
    result = deploy(api, spec)
    assert result["job"]["status"] == "ok", result["lines"]
    env = app_container(ctx, "scoped")["env"]
    assert env["RT"] == "1" and env["BOTH"] == "3" and "BLD" not in env
    assert installs and installs[0]["BLD"] == "2" and installs[0]["BOTH"] == "3"


def test_git_build_gets_only_build_and_both_as_args_without_values_in_log(api, ctx, monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    monkeypatch.setattr(git_build, "checkout", lambda *args, **kwargs: (repo, "abcdef1234567890"))
    commands = []
    monkeypatch.setattr(git_build, "run_streaming", lambda job, cmd, cwd=None, env=None, shown=None: commands.append((cmd, shown)))
    spec = {
        "slug": "gitapp", "source": {"kind": "git", "git_url": "https://example.com/r.git"}, "container_port": 3000,
        "env": {"RT": "1", "BLD": "tajny-2", "BOTH": "3"}, "env_scopes": {"BLD": "build", "BOTH": "both"},
    }  # fmt: skip
    result = deploy(api, spec)
    assert result["job"]["status"] == "ok", result["lines"]
    cmd, shown = commands[0]
    args = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--build-arg"]
    assert args == ["BLD=tajny-2", "BOTH=3"]
    assert "tajny-2" not in shown and "BLD=***" in shown
    assert not any("tajny-2" in line for line in result["lines"])
    env = app_container(ctx, "gitapp")["env"]
    assert env["RT"] == "1" and "BLD" not in env


def test_build_without_args_adds_no_build_arg(tmp_path, monkeypatch):
    (tmp_path / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    commands = []
    monkeypatch.setattr(git_build, "run_streaming", lambda job, cmd, cwd=None, env=None, shown=None: commands.append(cmd))
    git_build.build(object(), tmp_path, "Dockerfile", ".", "deployra/x:1")
    assert "--build-arg" not in commands[0] and commands[0][-1] == str(tmp_path)

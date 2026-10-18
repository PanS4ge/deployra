from deployra_backend.services import docker_svc


def _running(ctx, name):
    fake = ctx.docker_for("local")
    cid = fake.api.create_container("alpine:3.20", name=name)["Id"]
    fake.api.start(cid)
    return fake, cid


def test_exec_command_reports_exit_and_output(ctx):
    fake, cid = _running(ctx, "job-direct")
    res = docker_svc.exec_command(fake, cid, ["sh", "-c", "echo hi"], 60)
    assert res["exit_code"] == 0 and res["timed_out"] is False
    assert isinstance(res["output"], str) and res["duration"] >= 0


def test_exec_command_wraps_with_timeout_binary(ctx, monkeypatch):
    fake, cid = _running(ctx, "job-wrap")
    seen = []

    def fake_exec(client, container, cmd):
        seen.append(cmd)
        return 0, "x" * (docker_svc.EXEC_OUTPUT_LIMIT + 10)

    monkeypatch.setattr(docker_svc, "_exec", fake_exec)
    res = docker_svc.exec_command(fake, cid, ["sh", "-c", "sleep 1"], 90)
    assert seen[0][:4] == ["sh", "-c", docker_svc.EXEC_WITH_TIMEOUT, "--"] and seen[0][4:] == ["90", "sh", "-c", "sleep 1"]
    assert len(res["output"]) == docker_svc.EXEC_OUTPUT_LIMIT  # ogon wyniku


def test_exec_command_marks_killed_after_limit(ctx, monkeypatch):
    fake, cid = _running(ctx, "job-kill")
    monkeypatch.setattr(docker_svc, "_exec", lambda client, container, cmd: (137, "killed"))
    assert docker_svc.exec_command(fake, cid, ["sh", "-c", "x"], 1)["timed_out"] is True
    assert docker_svc.exec_command(fake, cid, ["sh", "-c", "x"], 300)["timed_out"] is False  # zabity przez OOM, nie limit


def test_exec_run_endpoint(api, ctx):
    fake, cid = _running(ctx, "job-api")
    r = api.request("POST", f"/api/containers/{cid}/exec/run", json={"cmd": ["sh", "-c", "echo hi"], "timeout": 60})
    assert r.status_code == 200
    assert r.json()["exit_code"] == 0 and "timed_out" in r.json()
    bad = api.request("POST", f"/api/containers/{cid}/exec/run", json={"cmd": [], "timeout": 60})
    assert bad.status_code == 422
    too_long = api.request("POST", f"/api/containers/{cid}/exec/run", json={"cmd": ["true"], "timeout": 99999})
    assert too_long.status_code == 422

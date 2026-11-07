"""Węzeł: blokada parowania per adres i token gita poza linią poleceń."""

import pytest

from deployra_backend import enroll
from deployra_backend.services import git_build


def test_pairing_lockout_is_per_ip():
    gate = enroll.PairingGate()
    for _ in range(enroll.MAX_FAILURES):
        gate.fail("198.51.100.7")
    with pytest.raises(PermissionError):
        gate.check("198.51.100.7")
    gate.check("10.66.0.2")  # panel z innego adresu nadal sparuje węzeł


def test_git_token_goes_through_env_not_argv(monkeypatch):
    seen = {}

    def fake(job, cmd, cwd=None, env=None, shown=None):
        seen.update(cmd=cmd, env=env)

    monkeypatch.setattr(git_build, "run_streaming", fake)
    git_build._git(None, ["fetch"], None, {}, "ghp_sekret", "git fetch")
    assert not any("ghp_" in part or "Authorization" in part for part in seen["cmd"])
    assert seen["env"]["GIT_CONFIG_KEY_0"] == "http.extraHeader" and seen["env"]["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")


def test_lan_subnets_apply_live_without_fixed_interface(monkeypatch):
    from deployra_backend.services import wg_spoke

    calls = []

    def runner(cmd, **_):
        calls.append(cmd)

        class R:
            returncode = 1 if "-C" in cmd else 0  # reguł jeszcze nie ma

        return R()

    monkeypatch.setattr(wg_spoke.wg.WgControl, "tools_available", staticmethod(lambda: False))
    old = {"subnet": "10.66.0.0/24", "lan_subnets": ["192.168.1.0/24"]}
    new = {**old, "lan_subnets": ["10.8.0.0/24"]}
    assert wg_spoke.apply_lan(old, new, "wg0", runner=runner)
    flat = [" ".join(c) for c in calls]
    assert "iptables -t nat -D POSTROUTING -s 10.66.0.0/24 -d 192.168.1.0/24 -j MASQUERADE" in flat
    assert "iptables -t nat -A POSTROUTING -s 10.66.0.0/24 -d 10.8.0.0/24 -j MASQUERADE" in flat
    assert "iptables -A FORWARD -i wg0 -j ACCEPT" in flat
    with pytest.raises(ValueError):
        wg_spoke.check_lan(old, ["10.66.0.0/16"])  # nachodzi na tunel


def test_lan_and_probe_api(api, ctx, monkeypatch):
    from deployra_backend.services import wg_spoke

    ctx.state.update(wg={"subnet": "10.66.0.0/24", "address": "10.66.0.5", "lan_subnets": []})
    monkeypatch.setattr(wg_spoke, "apply_lan", lambda old, new, iface: True)
    saved = api.request("PUT", "/api/wg/lan", json={"lan_subnets": ["10.8.0.7/24"]}).json()
    assert saved == {"ok": True, "lan_subnets": ["10.8.0.0/24"], "applied": True} and ctx.state.get("wg")["lan_subnets"] == ["10.8.0.0/24"]
    assert api.request("POST", "/api/net/probe", json={"host": "8.8.8.8", "port": 53}).status_code == 400  # publiczny adres — nie
    result = api.request("POST", "/api/net/probe", json={"host": "10.8.0.250", "port": 9}).json()
    assert result["ok"] is False and "error" in result


def test_terminal_websocket_is_nonblocking_echo_with_ping(api, ctx):
    from deployra_common.hmac_auth import sign

    cid = ctx.docker.api.create_container("alpine", name="term")["Id"]
    ctx.docker.api.start(cid)
    path = f"/api/containers/{cid[:12]}/exec?shell=sh&cols=80&rows=24&user=root&workdir=/tmp"
    with api.client.websocket_connect(path, headers=sign(api.secret, "GET", path, b"")) as ws:
        assert b"atrapa$" in ws.receive_bytes()
        ws.send_text('{"type": "ping"}')  # podtrzymanie — bez odpowiedzi i bez błędu
        ws.send_text('{"type": "resize", "cols": 100, "rows": 30}')
        ws.send_bytes(b"ls\r")
        assert b"ls" in ws.receive_bytes()
        ws.send_bytes(b"exit\r")
        assert '"exit"' in ws.receive_text()
    bad = f"/api/containers/{cid[:12]}/exec?workdir=relative"
    with pytest.raises(Exception):
        with api.client.websocket_connect(bad, headers=sign(api.secret, "GET", bad, b"")) as ws:
            ws.receive_bytes()

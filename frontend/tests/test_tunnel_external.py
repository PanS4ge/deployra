from urllib.parse import unquote

from deployra_common.client import RemoteError


class FakeGateway:
    def __init__(self):
        self.calls = []

    async def get(self, path, **_):
        self.calls.append(("GET", path))
        return {"tunnels": [{"name": "wg1", "addresses": ["10.8.0.4/24"], "routes": ["10.8.0.0/24"], "endpoint": "cezary.example.com:30178",
                             "live_endpoint": "1.2.3.4:30178", "up": True, "online": True, "handshake_age": 12, "latest_handshake": 1,
                             "rx_bytes": 2048, "tx_bytes": 1024, "ignored": ["dns"], "error": None}]}  # fmt: skip

    async def put(self, path, json=None, **_):
        self.calls.append(("PUT", path, json))
        if "zla" in path:
            raise RemoteError(400, "nazwa interfejsu: „wg” + litery/cyfry")
        return {"name": "wg1", "routes": json["routes"] or ["10.8.0.0/24"], "ignored": ["dns", "postup"], "error": None}

    async def delete(self, path, **_):
        self.calls.append(("DELETE", path))
        return {"ok": True}


def test_external_wireguard_card(admin, monkeypatch):
    panel = admin.app.state.panel
    gateway = FakeGateway()
    monkeypatch.setattr(panel, "gateway_client", lambda: gateway)

    page = admin.get("/tunnel/external").text
    assert "wg1" in page and "10.8.0.0/24" in page and "cezary.example.com:30178" in page and "dot ok" in page

    response = admin.post("/tunnel/external", data={"name": "wg1", "config": "[Interface]\n…", "routes": "10.8.0.0/24, 192.168.5.0/24"})
    assert response.status_code == 204 and "DNS, POSTUP" in unquote(response.headers["set-cookie"])
    assert gateway.calls[-1] == ("PUT", "/api/external-wg/wg1", {"config": "[Interface]\n…", "routes": ["10.8.0.0/24", "192.168.5.0/24"]})
    bad = admin.post("/tunnel/external", data={"name": "zla nazwa", "config": "x"})
    assert bad.status_code == 400 and gateway.calls[-1][1] == "/api/external-wg/zla%20nazwa"

    assert admin.post("/tunnel/external/wg1/delete").status_code == 204
    assert gateway.calls[-1] == ("DELETE", "/api/external-wg/wg1")

    monkeypatch.setattr(panel, "gateway_client", lambda: None)
    assert "Brak" in admin.get("/tunnel/external").text

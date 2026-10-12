import datetime
import json

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from deployra_common import pairing, wg
from deployra_common.hmac_auth import sign
from deployra_common.schemas import CertRequest
from deployra_gateway import certs
from deployra_gateway.app import create_api_app, create_bootstrap_app


def _signed(client, secret, method, path, body=None):
    raw = json.dumps(body).encode() if body is not None else b""
    headers = sign(secret, method, path, raw)
    if body is not None:
        headers["Content-Type"] = "application/json"
    return client.request(method, path, content=raw, headers=headers)


def _pair(boot, ctx, with_wg=True):
    code = ctx.ensure_pairing_code()
    x_priv, x_pub = pairing.new_keypair()
    _, wg_pub = wg.genkey()
    body = {"kind": "pair-gateway", "x25519": x_pub, "nonce": "abc", "name": "Panel"}
    if with_wg:
        body["wg_public_key"] = wg_pub
    response = boot.post("/pair", json=pairing.seal(code, body))
    assert response.status_code == 200, response.text
    data = pairing.unseal(code, response.json(), "pair-gateway-ok")
    return pairing.derive_secret(x_priv, data["x25519"], code, "panel-gateway"), data


def test_full_sync_join_and_proxy_flow(ctx):
    boot = TestClient(create_bootstrap_app(ctx))
    api = TestClient(create_api_app(ctx, start_scheduler=False))

    assert boot.get("/health").json()["paired"] is False
    secret, data = _pair(boot, ctx)
    assert data["nonce"] == "abc" and data["tunnel"]["address"] == "10.66.0.2"
    assert data["tunnel"]["hub_endpoint"] == "vps.example.com:51820"
    assert data["gateway"]["api_url"] == "http://10.66.0.1:2751"
    # drugi raz się nie da
    assert boot.post("/pair", json={"kind": "pair-gateway"}).status_code == 409

    # API bez podpisu → 401
    assert api.get("/api/status").status_code == 401

    # panel rejestruje token, węzeł dołącza
    token = pairing.JoinToken.generate()
    r = _signed(api, secret, "POST", "/api/join-tokens",
                {"id": token.id, "join_secret": token.join_secret, "name": "dom", "lan_subnets": ["192.168.1.0/24"]})
    assert r.status_code == 200, r.text
    _, node_pub = wg.genkey()
    join = pairing.seal(token.join_secret, {"kind": "join", "token_id": token.id, "wg_public_key": node_pub, "nonce": "n"})
    response = boot.post("/join", json=join)
    assert response.status_code == 200, response.text
    joined = pairing.unseal(token.join_secret, response.json(), "join-ok")
    assert joined["tunnel"]["address"] == "10.66.0.3" and joined["lan_subnets"] == ["192.168.1.0/24"]
    # ponowienie tym samym kluczem — ta sama konfiguracja; inny klucz — odmowa
    assert pairing.unseal(token.join_secret, boot.post("/join", json=join).json(), "join-ok")["tunnel"]["address"] == "10.66.0.3"
    _, other = wg.genkey()
    hijack = pairing.seal(token.join_secret, {"kind": "join", "token_id": token.id, "wg_public_key": other, "nonce": "z"})
    assert boot.post("/join", json=hijack).status_code == 409
    # zły sekret
    forged = pairing.seal("zly-sekret-123456789", {"kind": "join", "token_id": token.id, "wg_public_key": other})
    assert boot.post("/join", json=forged).status_code == 403

    status = _signed(api, secret, "GET", "/api/status").json()
    peers = {p["id"]: p for p in status["peers"]}
    assert peers["panel"]["kind"] == "panel" and peers[token.id]["lan_subnets"] == ["192.168.1.0/24"]

    # konfiguracja wg huba zawiera oba peery z właściwymi AllowedIPs
    conf = wg.render_quick(ctx.hub.interface(), ctx.hub.peers())
    assert "AllowedIPs = 10.66.0.3/32, 192.168.1.0/24" in conf and "ListenPort = 51820" in conf
    assert "PostUp = iptables -I FORWARD -i %i -o %i -j ACCEPT" in conf  # węzły widzą się nawzajem przez hub

    # nakładające się podsieci LAN są odrzucane
    t2 = pairing.JoinToken.generate()
    _signed(api, secret, "POST", "/api/join-tokens", {"id": t2.id, "join_secret": t2.join_secret, "lan_subnets": ["192.168.1.128/25"]})
    _, pub2 = wg.genkey()
    clash = boot.post("/join", json=pairing.seal(t2.join_secret, {"kind": "join", "token_id": t2.id, "wg_public_key": pub2}))
    assert clash.status_code == 400 and "routowana" in clash.json()["detail"]

    # proxy: zapis stanu
    proxy = {"sites": [{"id": 1, "domains": ["app.example.com"], "upstream": {"host": "10.66.0.3", "port": 20001}}]}
    result = _signed(api, secret, "PUT", "/api/proxy", proxy).json()
    assert result["status"] == "written"
    assert "proxy_pass http://10.66.0.3:20001;" in (ctx.config.nginx_dir / "http" / "site-1.conf").read_text()
    bad = {"sites": [{"id": 1, "domains": ["x.com;evil"], "upstream": {"host": "10.66.0.3", "port": 1}}]}
    assert _signed(api, secret, "PUT", "/api/proxy", bad).status_code == 422

    # usunięcie węzła
    assert _signed(api, secret, "DELETE", f"/api/peers/{token.id}").json() == {"ok": True}
    assert token.id not in {p["id"] for p in _signed(api, secret, "GET", "/api/status").json()["peers"]}


def _self_signed(domain: str):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=60))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(domain)]), critical=False)
        .sign(key, hashes.SHA256())
    )  # fmt: skip
    pem_key = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return cert.public_bytes(serialization.Encoding.PEM).decode(), pem_key.decode()


def test_cert_upload_and_render(ctx):
    boot = TestClient(create_bootstrap_app(ctx))
    api = TestClient(create_api_app(ctx, start_scheduler=False))
    secret, _ = _pair(boot, ctx, with_wg=False)
    chain, key = _self_signed("app.example.com")
    _, other_key = _self_signed("x.example.com")
    assert _signed(api, secret, "POST", "/api/certs/upload", {"id": 4, "fullchain": chain, "privkey": other_key}).status_code == 400
    response = _signed(api, secret, "POST", "/api/certs/upload", {"id": 4, "fullchain": chain, "privkey": key})
    assert response.status_code == 200, response.text
    assert response.json()["cert"]["domains"] == ["app.example.com"] and response.json()["cert"]["days_left"] >= 59
    proxy = {"sites": [{"id": 1, "domains": ["app.example.com"], "upstream": {"host": "10.66.0.3", "port": 20001}, "cert_id": 4}]}
    _signed(api, secret, "PUT", "/api/proxy", proxy)
    assert "ssl_certificate /etc/deployra/certs/4/fullchain.pem;" in (ctx.config.nginx_dir / "http" / "site-1.conf").read_text()
    assert _signed(api, secret, "GET", "/api/certs").json()[0]["challenge"] == "custom"


def test_lego_commands(ctx):
    http = CertRequest(id=1, domains=["a.example.com", "www.a.example.com"], email="a@b.pl", staging=True)
    cmd, env = certs.lego_command("lego", ctx.config.data_dir, ctx.config.acme_dir, http, "run")
    assert cmd[-1] == "run" and "--http.webroot" in cmd and "--server" in cmd and env == {}
    assert cmd.count("--domains") == 2
    dns = CertRequest(id=2, domains=["*.example.com"], email="a@b.pl", challenge="dns-cloudflare", cloudflare_token="tok")
    cmd, env = certs.lego_command("lego", ctx.config.data_dir, ctx.config.acme_dir, dns, "renew")
    assert ["--dns", "cloudflare"] == cmd[cmd.index("--dns"):cmd.index("--dns") + 2]
    assert env == {"CF_DNS_API_TOKEN": "tok"} and cmd[-3:] == ["renew", "--days", "30"]
    assert "tok" not in " ".join(cmd)


def test_pairing_lockout(ctx):
    boot = TestClient(create_bootstrap_app(ctx))
    ctx.ensure_pairing_code()
    _, pub = pairing.new_keypair()
    bad = pairing.seal("DPLR-AAAA-BBBB-CCCC", {"kind": "pair-gateway", "x25519": pub})
    for _ in range(10):
        assert boot.post("/pair", json=bad).status_code == 403
    assert "zbyt wiele" in boot.post("/pair", json=bad).json()["detail"]


@pytest.mark.parametrize("subnet", ["10.66.0.0/25", "not-a-subnet"])
def test_token_rejects_bad_subnets(ctx, subnet):
    with pytest.raises(ValueError):
        ctx.register_token("abc123", "s" * 20, "x", [subnet], 60)
        ctx.hub.add_peer("abc123", "node", "x", wg.genkey()[1], [subnet])


def test_check_batch(ctx):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.headers.get("Host") == "ok.example.com" else 503)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    boot = TestClient(create_bootstrap_app(ctx))
    api = TestClient(create_api_app(ctx, start_scheduler=False))
    secret, _ = _pair(boot, ctx, with_wg=False)
    items = [
        {"id": "1", "url": f"http://127.0.0.1:{port}/", "host": "ok.example.com"},
        {"id": "2", "url": f"http://127.0.0.1:{port}/", "host": "zle.example.com"},
        {"id": "3", "url": "http://127.0.0.1:1/", "host": None},
        {"id": "4", "url": "ftp://x/", "host": None},
    ]
    results = _signed(api, secret, "POST", "/api/check/batch", {"items": items}).json()["results"]
    server.shutdown()
    assert results["1"]["ok"] and results["1"]["status"] == 200 and "ms" in results["1"]
    assert results["2"]["status"] == 503 and not results["2"]["ok"]
    assert results["3"]["status"] is None and not results["3"]["ok"]
    assert not results["4"]["ok"]

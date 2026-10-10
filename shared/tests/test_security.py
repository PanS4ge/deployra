import base64
import time

import pytest

from deployra_common import pairing, wg
from deployra_common.hmac_auth import NonceCache, SignatureError, sign, verify

SECRET = b"s" * 32


def test_sign_verify_roundtrip_with_query():
    headers = sign(SECRET, "post", "/api/x?a=1&b=2", b'{"x":1}', key_id="panel")
    assert verify(SECRET, "POST", "/api/x?a=1&b=2", b'{"x":1}', headers, nonces=NonceCache()) == "panel"


@pytest.mark.parametrize(
    "method,path,body,secret",
    [
        ("POST", "/api/x?a=1&b=2", b'{"x":2}', SECRET),  # zmienione body
        ("POST", "/api/x?a=1&b=3", b'{"x":1}', SECRET),  # zmienione query
        ("GET", "/api/x?a=1&b=2", b'{"x":1}', SECRET),  # zmieniona metoda
        ("POST", "/api/x?a=1&b=2", b'{"x":1}', b"z" * 32),  # inny sekret
    ],
)
def test_verify_rejects_tampering(method, path, body, secret):
    headers = sign(SECRET, "POST", "/api/x?a=1&b=2", b'{"x":1}')
    with pytest.raises(SignatureError):
        verify(secret, method, path, body, headers, nonces=None)


def test_verify_rejects_old_timestamp_and_replay():
    old = sign(SECRET, "GET", "/a", ts=int(time.time()) - 500)
    with pytest.raises(SignatureError, match="okn"):
        verify(SECRET, "GET", "/a", b"", old, nonces=None)
    nonces = NonceCache()
    fresh = sign(SECRET, "GET", "/a")
    verify(SECRET, "GET", "/a", b"", fresh, nonces=nonces)
    with pytest.raises(SignatureError, match="powtórz"):
        verify(SECRET, "GET", "/a", b"", fresh, nonces=nonces)


def test_streamed_body_uses_declared_digest():
    headers = sign(SECRET, "POST", "/up", body_sha256="ab" * 32)
    assert verify(SECRET, "POST", "/up", None, headers, nonces=None) == "panel"
    headers["X-Deployra-Body-Sha256"] = "cd" * 32
    with pytest.raises(SignatureError):
        verify(SECRET, "POST", "/up", None, headers, nonces=None)


def test_pairing_derives_same_secret_and_detects_mitm():
    code = pairing.new_code()
    a_priv, a_pub = pairing.new_keypair()
    b_priv, b_pub = pairing.new_keypair()
    request = pairing.seal(code, {"kind": "pair-gateway", "x25519": a_pub})
    assert pairing.unseal(code, request, "pair-gateway")["x25519"] == a_pub
    assert pairing.derive_secret(a_priv, b_pub, code, "gw") == pairing.derive_secret(b_priv, a_pub, code, "gw")
    assert pairing.derive_secret(a_priv, b_pub, code, "gw") != pairing.derive_secret(a_priv, b_pub, code, "node")

    # MITM podmienia klucz, ale nie zna kodu
    _, evil_pub = pairing.new_keypair()
    forged = {**request, "x25519": evil_pub}
    with pytest.raises(ValueError):
        pairing.unseal(code, forged, "pair-gateway")
    with pytest.raises(ValueError):
        pairing.unseal("DPLR-AAAA-BBBB-CCCC", request, "pair-gateway")
    with pytest.raises(ValueError):
        pairing.unseal(code, request, "pair-node")


def test_code_and_join_token_format():
    code = pairing.new_code()
    assert code.startswith("DPLR-") and len(code) == 19
    assert pairing.normalize_code(" " + code[5:].lower()) == code
    token = pairing.JoinToken.generate()
    assert pairing.JoinToken.parse(token.encode()) == token
    with pytest.raises(ValueError):
        pairing.JoinToken.parse("dplr1.abc.def")


def test_wg_keys_match_rfc7748_vector():
    # RFC 7748 §6.1: klucz prywatny Alice → publiczny (WireGuard używa tej samej krzywej)
    alice = base64.b64encode(bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")).decode()
    expected = bytes.fromhex("8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a")
    assert base64.b64decode(wg.pubkey(alice)) == expected
    priv, pub = wg.genkey()
    assert wg.pubkey(priv) == pub and wg.valid_key(pub) and wg.valid_key(wg.genpsk())

from deployra_common import totp


def test_rfc6238_vector():
    # RFC 6238, dodatek B: sekret ASCII "12345678901234567890", T=59 s → 94287082 (8 cyfr) → 287082 (6 cyfr)
    import base64

    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp.code_at(secret, 59 // 30) == "287082"


def test_verify_drift_and_replay():
    secret = totp.new_secret()
    at = 1_800_000_000.0
    code = totp.code_at(secret, totp.now_step(at))
    step = totp.verify(secret, code, at=at)
    assert step == totp.now_step(at)
    assert totp.verify(secret, code, last_step=step, at=at) is None  # drugi raz ten sam kod — nie
    assert totp.verify(secret, totp.code_at(secret, step - 1), at=at) == step - 1  # zegar telefonu 30 s do tyłu
    assert totp.verify(secret, totp.code_at(secret, step - 3), at=at) is None
    assert totp.verify(secret, "12 34 5", at=at) is None and totp.verify(None, code, at=at) is None


def test_uri_and_backup_codes():
    assert totp.uri("ABC", "jan@example.com").startswith("otpauth://totp/Deployra%3Ajan%40example.com?secret=ABC")
    codes = totp.backup_codes()
    assert len(codes) == 10 and len(set(codes)) == 10 and all(len(c) == 9 for c in codes)

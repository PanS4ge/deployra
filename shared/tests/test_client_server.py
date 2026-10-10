import httpx
import pytest
from fastapi import Depends, FastAPI, Request

from deployra_common.client import RemoteError, SignedClient
from deployra_common.server import SignatureGuard, receive_verified_upload

SECRET = b"k" * 32


def make_app(tmp_path):
    guard = SignatureGuard(lambda key_id: SECRET if key_id == "panel" else None)
    app = FastAPI()

    @app.get("/api/echo")
    async def echo(q: str, who: str = Depends(guard)):
        return {"q": q, "who": who}

    @app.post("/api/data")
    async def data(request: Request, who: str = Depends(guard)):
        return {"got": await request.json()}

    @app.post("/api/upload")
    async def upload(request: Request, who: str = Depends(guard.upload)):
        size = await receive_verified_upload(request, str(tmp_path / "out.bin"), 10_000_000)
        return {"size": size}

    return app


@pytest.mark.anyio
async def test_signed_roundtrip(tmp_path):
    transport = httpx.ASGITransport(app=make_app(tmp_path))
    client = SignedClient("http://node", SECRET, transport=transport)
    assert await client.get("/api/echo", params={"q": "zażółć gęślą"}) == {"q": "zażółć gęślą", "who": "panel"}
    assert await client.post("/api/data", json={"a": [1, 2]}) == {"got": {"a": [1, 2]}}

    src = tmp_path / "in.bin"
    src.write_bytes(b"x" * 3_000_000)
    assert await client.upload_file("/api/upload", str(src)) == {"size": 3_000_000}
    assert (tmp_path / "out.bin").read_bytes() == src.read_bytes()


@pytest.mark.anyio
async def test_unsigned_and_wrong_secret_rejected(tmp_path):
    transport = httpx.ASGITransport(app=make_app(tmp_path))
    async with httpx.AsyncClient(transport=transport, base_url="http://node") as raw:
        assert (await raw.get("/api/echo", params={"q": "x"})).status_code == 401
    wrong = SignedClient("http://node", b"z" * 32, transport=transport)
    with pytest.raises(RemoteError) as info:
        await wrong.get("/api/echo", params={"q": "x"})
    assert info.value.status == 401

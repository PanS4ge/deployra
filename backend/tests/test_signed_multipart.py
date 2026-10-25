from deployra_common.client import SignedClient


def test_signed_multipart_upload_reaches_route(api, ctx):
    """Panel wysyła pliki jako multipart podpisany w całości — węzeł nie może zgubić body przy weryfikacji podpisu."""
    cid = ctx.docker.api.create_container("nginx:latest", name="web", host_config={})["Id"]
    ctx.docker.api.start(cid)
    client = SignedClient("http://testserver", api.secret)
    request = client.build("POST", f"/api/containers/{cid}/files/upload", data={"dir_path": "/srv"},
                           files=[("files", ("a.txt", b"hello", "text/plain"))])  # fmt: skip
    response = api.client.request("POST", request.url.raw_path.decode(), content=request.read(), headers=dict(request.headers))
    assert response.status_code == 200, response.text
    assert response.json()["uploaded"] == [{"name": "a.txt", "size": 5}]


def test_tampered_multipart_is_rejected(api, ctx):
    cid = ctx.docker.api.create_container("nginx:latest", name="web", host_config={})["Id"]
    client = SignedClient("http://testserver", api.secret)
    request = client.build("POST", f"/api/containers/{cid}/files/upload", data={"dir_path": "/srv"},
                           files=[("files", ("a.txt", b"hello", "text/plain"))])  # fmt: skip
    body = request.read().replace(b"hello", b"HACKD")
    response = api.client.request("POST", request.url.raw_path.decode(), content=body, headers=dict(request.headers))
    assert response.status_code == 401

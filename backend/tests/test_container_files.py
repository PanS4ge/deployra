import pytest
from deployra_backend.services import docker_svc


def test_docker_svc_files_direct(ctx):
    fake = ctx.docker_for("local")
    # Tworzymy kontener
    cid = fake.api.create_container("nginx:alpine", name="c-files")["Id"]

    # 1. Listowanie katalogu
    res = docker_svc.list_container_files(fake, cid, "/")
    assert res["path"] == "/"
    assert len(res["items"]) >= 2
    names = {i["name"] for i in res["items"]}
    assert "var" in names or "index.html" in names

    # 2. Zapis pliku
    write_res = docker_svc.write_container_file(fake, cid, "/app/config.txt", "hello container")
    assert write_res["ok"] is True
    assert write_res["size"] == 15

    # 3. Odczyt pliku
    read_res = docker_svc.read_container_file(fake, cid, "/app/config.txt")
    assert read_res["path"] == "/app/config.txt"
    assert read_res["content"] == "hello container"
    assert read_res["is_binary"] is False

    # 4. Pobieranie surowych bajtów
    name, raw_data = docker_svc.get_container_file_bytes(fake, cid, "/app/config.txt")
    assert name == "config.txt"
    assert raw_data == b"hello container"

    # 5. Tworzenie katalogu i usuwanie
    mkdir_res = docker_svc.mkdir_container(fake, cid, "/app/newdir")
    assert mkdir_res["ok"] is True

    del_res = docker_svc.delete_container_file(fake, cid, "/app/config.txt")
    assert del_res["ok"] is True


def test_container_files_api(api, ctx):
    fake = ctx.docker_for("local")
    cid = fake.api.create_container("nginx:alpine", name="api-files")["Id"]

    # 1. GET /api/containers/{cid}/files
    r = api.request("GET", f"/api/containers/{cid}/files?path=/etc")
    assert r.status_code == 200
    data = r.json()
    assert data["path"] == "/etc"
    assert len(data["items"]) >= 2

    # 2. POST /api/containers/{cid}/files/write
    r_w = api.request("POST", f"/api/containers/{cid}/files/write", json={"path": "/etc/custom.conf", "content": "key=val"})
    assert r_w.status_code == 200
    assert r_w.json()["ok"] is True

    # 3. GET /api/containers/{cid}/files/read
    r_r = api.request("GET", f"/api/containers/{cid}/files/read?path=/etc/custom.conf")
    assert r_r.status_code == 200
    assert r_r.json()["content"] == "key=val"

    # 4. GET /api/containers/{cid}/files/download
    r_d = api.request("GET", f"/api/containers/{cid}/files/download?path=/etc/custom.conf")
    assert r_d.status_code == 200
    assert r_d.content == b"key=val"

    # 5. POST /api/containers/{cid}/files/mkdir
    r_m = api.request("POST", f"/api/containers/{cid}/files/mkdir", json={"path": "/etc/subfolder"})
    assert r_m.status_code == 200
    assert r_m.json()["ok"] is True

    # 6. DELETE /api/containers/{cid}/files
    r_del = api.request("DELETE", f"/api/containers/{cid}/files?path=/etc/custom.conf")
    assert r_del.status_code == 200
    assert r_del.json()["ok"] is True

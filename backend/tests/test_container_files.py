import pytest
from deployra_backend.services import docker_svc


def test_docker_svc_files_direct(ctx):
    fake = ctx.docker_for("local")
    # Tworzymy kontener
    cid = fake.api.create_container("nginx:alpine", name="c-files")["Id"]
    fake.api.start(cid)

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
    fake.api.start(cid)

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


def test_stopped_container_lists_mounts_through_helper(ctx):
    fake = ctx.docker_for("local")
    cid = fake.api.create_container("itzg/minecraft-server", name="mc")["Id"]  # nieuruchomiony = zatrzymany

    root = docker_svc.list_container_files(fake, cid, "/")
    assert root["mode"] == "mounts" and root["running"] is False
    assert [i["path"] for i in root["items"]] == ["/data"] and root["items"][0]["type"] == "mount"

    inside = docker_svc.list_container_files(fake, cid, "/data")
    assert {i["name"] for i in inside["items"]} == {"world", "server.properties"}
    run = fake.api.oneoff_runs[-1]
    assert run["image"] == docker_svc.FILES_HELPER and run["volumes_from"] == [cid]
    assert not any(c["labels"].get("deployra.system") == "files" for c in fake.api.containers_.values())  # pomocnik sprzątnięty


def test_stopped_container_reads_bind_from_srv_directly(ctx, monkeypatch):
    fake = ctx.docker_for("local")
    site = ctx.config.srv_dir / "sites" / "blog"
    (site / "public").mkdir(parents=True)
    (site / "index.php").write_text("<?php echo 1;", encoding="utf-8")
    cid = fake.api.create_container("php:8.3-apache", name="blog")["Id"]
    original = fake.api.inspect_container

    def inspect(c):
        info = original(c)
        info["Mounts"] = [{"Type": "bind", "Source": str(site), "Destination": "/var/www/html", "RW": True}]
        return info

    monkeypatch.setattr(fake.api, "inspect_container", inspect)
    listing = docker_svc.list_container_files(fake, cid, "/var/www/html", ctx.config.srv_dir)
    assert {i["name"]: i["type"] for i in listing["items"]} == {"public": "dir", "index.php": "file"}
    assert not fake.api.oneoff_runs  # bez kontenera pomocniczego


def test_stopped_container_refuses_paths_outside_mounts(api, ctx):
    fake = ctx.docker_for("local")
    cid = fake.api.create_container("nginx:alpine", name="stopped")["Id"]
    r = api.request("POST", f"/api/containers/{cid}/files/mkdir", json={"path": "/etc/new"})
    assert r.status_code == 409
    r = api.request("POST", f"/api/containers/{cid}/files/mkdir", json={"path": "/data/plugins"})
    assert r.status_code == 200 and fake.api.oneoff_runs[-1]["command"] == ["mkdir", "-p", "--", "/data/plugins"]


def test_write_keeps_owner_and_mode(ctx):
    import io
    import tarfile

    fake = ctx.docker_for("local")
    cid = fake.api.create_container("nginx:alpine", name="owner")["Id"]
    fake.api.start(cid)

    def archive(c, path):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo("run.sh")
            info.size, info.uid, info.gid, info.mode = 3, 1000, 1001, 0o755
            tar.addfile(info, io.BytesIO(b"old"))
        return [buf.getvalue()], {"name": "run.sh", "size": 3}

    sent = []
    fake.api.get_archive = archive
    fake.api.put_archive = lambda c, path, data: sent.append(data)
    docker_svc.write_container_file(fake, cid, "/app/run.sh", "echo hi")
    member = tarfile.open(fileobj=io.BytesIO(sent[0])).next()
    assert (member.uid, member.gid, member.mode) == (1000, 1001, 0o755)


def test_folder_upload_tree_and_zip_download(ctx):
    import io
    import zipfile

    fake = ctx.docker_for("local")
    cid = fake.api.create_container("python:3.11-slim", name="c-tree")["Id"]
    fake.api.start(cid)

    done = docker_svc.upload_container_tree(fake, cid, "/app", [("site/index.html", b"<h1>hi</h1>"), ("site/css/a.css", b"body{}")])
    assert [d["name"] for d in done] == ["site/index.html", "site/css/a.css"]

    name, data = docker_svc.get_container_file_bytes(fake, cid, "/app/site")
    assert name == "site.zip"
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert zf.read("site/index.html") == b"<h1>hi</h1>"
    assert zf.read("site/css/a.css") == b"body{}"


def test_folder_upload_rejects_parent_paths(ctx):
    fake = ctx.docker_for("local")
    cid = fake.api.create_container("python:3.11-slim", name="c-evil")["Id"]
    with pytest.raises(ValueError):
        docker_svc.upload_container_tree(fake, cid, "/app", [("x/../../etc/passwd", b"x")])


def test_processes_action_has_proc_fallback():
    from deployra_common import container_profiles

    profile = container_profiles.detect_profile("python:3.11-slim")
    cmd, _, _ = container_profiles.build_action_command(profile, "processes")
    assert "/proc/" in cmd[-1] and "\x00" not in cmd[-1]


def _tar(entries):
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif isinstance(data, str):  # dowiązanie symboliczne
                info.type, info.linkname = tarfile.SYMTYPE, data
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_upload_tar_streams_folder_into_container(ctx, tmp_path):
    fake = ctx.docker_for("local")
    cid = fake.api.create_container("itzg/minecraft-server", name="mc")["Id"]
    archive = tmp_path / "up.tar"
    archive.write_bytes(_tar([("plugins", None), ("plugins/Essentials.jar", b"jar-2"), ("plugins/Essentials/config.yml", b"a: 1")]))
    done = docker_svc.upload_container_tar(fake, cid, "/data", str(archive))
    assert [d["name"] for d in done] == ["plugins/Essentials.jar", "plugins/Essentials/config.yml"]
    assert fake.api._files["/data/plugins/Essentials.jar"] == b"jar-2"


@pytest.mark.parametrize("entries", [[("../etc/passwd", b"x")], [("plugins/link", "/etc/shadow")]])
def test_upload_tar_rejects_unsafe_entries(ctx, tmp_path, entries):
    fake = ctx.docker_for("local")
    cid = fake.api.create_container("itzg/minecraft-server", name="mc2")["Id"]
    archive = tmp_path / "bad.tar"
    archive.write_bytes(_tar(entries))
    with pytest.raises(ValueError):
        docker_svc.upload_container_tar(fake, cid, "/data", str(archive))


def test_upload_tar_route_needs_signed_digest(api, ctx):
    import hashlib

    from deployra_common.hmac_auth import sign

    fake = ctx.docker_for("local")
    cid = fake.api.create_container("itzg/minecraft-server", name="mc3")["Id"]
    body = _tar([("plugins/a.jar", b"A" * 1000)])
    path = f"/api/containers/{cid}/files/upload-tar?dir_path=/data&ep=local"
    headers = sign(api.secret, "POST", path, b"", body_sha256=hashlib.sha256(body).hexdigest())
    response = api.client.post(path, content=body, headers={**headers, "Content-Type": "application/octet-stream"})
    assert response.status_code == 200 and response.json()["uploaded"] == [{"name": "plugins/a.jar", "size": 1000}]
    assert fake.api._files["/data/plugins/a.jar"] == b"A" * 1000
    plain = sign(api.secret, "POST", path, body)  # zwykły podpis bez skrótu — ten router przyjmuje tylko wysyłki strumieniowe
    assert api.client.post(path, content=body, headers=plain).status_code == 400

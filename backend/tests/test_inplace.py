from deployra_backend.services import inplace


def run(api, cid, **body):
    response = api.request("POST", f"/api/containers/{cid}/update", json=body)
    assert response.status_code == 200, response.text
    return api.wait_job(response.json()["job"]["id"])


def make_container(ctx, image="php:8.3-apache", labels=None, name="errpage"):
    api = ctx.docker.api
    cid = api.create_container(image, name=name, environment={"PHP_VERSION": "8.3.12", "APP_KEY": "tajne"}, labels=labels or {},
                               host_config=api.create_host_config(port_bindings={"80/tcp": 8080}, restart_policy={"Name": "always"}))["Id"]  # fmt: skip
    api.start(cid)
    return cid


def by_name(ctx):
    return {c["name"]: c for c in ctx.docker.api.containers_.values()}


def test_stack_container_is_recreated_in_place(api, ctx):
    labels = {"com.docker.compose.project": "errpage-stack", "com.docker.compose.service": "web"}
    cid = make_container(ctx, labels=labels)
    result = run(api, cid, image="phpnew:8.4-apache")
    assert result["job"]["status"] == "ok", result["lines"]
    containers = by_name(ctx)
    assert "errpage-pre-update" not in containers  # sprzątanie: stara kopia usunięta
    new = containers["errpage"]
    assert new["id"] != cid and new["image"] == "phpnew:8.4-apache" and new["state"] == "running"
    assert new["ports"] == {"80/tcp": 8080}  # ten sam port hosta — nic go nie blokuje
    assert new["labels"] == labels  # Portainer dalej widzi kontener w stacku
    assert new["env"] == {"APP_KEY": "tajne"}  # PHP_VERSION ze starego obrazu nie przechodzi do nowego
    assert new["host_config"]["restart_policy"] == {"Name": "always"}
    assert any("errpage-stack" in line for line in result["lines"])  # ostrzeżenie o pliku stacka
    assert result["job"]["result"]["host_port"] == 8080


def test_failed_update_restores_old_container(api, ctx):
    cid = make_container(ctx)
    ctx.docker.api.crash_images.add("phpnew:broken")
    result = run(api, cid, image="phpnew:broken")
    assert result["job"]["status"] == "error"
    assert result["job"]["code"] == "DPL-INPLACE-START"
    assert any(line.startswith("⌁ DPL-INPLACE-START") for line in result["lines"])  # linia do zgłoszenia
    containers = by_name(ctx)
    assert set(containers) == {"errpage"}
    old = containers["errpage"]
    assert old["id"] == cid and old["state"] == "running" and old["host_config"]["restart_policy"] == {"Name": "always"}


def test_same_image_is_left_alone(api, ctx):
    cid = make_container(ctx, image="redis:7-alpine")
    result = run(api, cid, image="redis:7-alpine")
    assert result["job"]["status"] == "ok" and result["job"]["result"]["unchanged"]
    assert by_name(ctx)["errpage"]["id"] == cid


def test_deployra_container_is_refused(api, ctx):
    cid = make_container(ctx, labels={"deployra.system": "panel"})
    result = run(api, cid, image="phpnew:8.4-apache")
    assert result["job"]["status"] == "error" and result["job"]["code"] == "DPL-SYSTEM"
    assert by_name(ctx)["errpage"]["id"] == cid


def test_bad_image_and_missing_container(api, ctx):
    cid = make_container(ctx)
    assert api.request("POST", f"/api/containers/{cid}/update", json={"image": "Zły Obraz"}).status_code == 400
    assert api.request("POST", "/api/containers/nie-ma/update", json={"image": "nginx:1"}).status_code == 404


def test_new_config_drops_image_defaults_and_keeps_anonymous_volumes():
    info = {
        "Id": "abcdef1234567890", "Name": "/db",
        "Config": {"Image": "mysql:8.0", "Hostname": "abcdef123456", "Env": ["MYSQL_MAJOR=8.0", "MYSQL_ROOT_PASSWORD=x"],
                   "Cmd": ["mysqld"], "Entrypoint": ["docker-entrypoint.sh"], "WorkingDir": "/srv", "Labels": {"maintainer": "x", "mine": "1"},
                   "Volumes": {"/var/lib/mysql": {}}},
        "HostConfig": {"NetworkMode": "proxy", "Binds": ["/etc/db:/etc/mysql/conf.d:ro"], "PortBindings": {"3306/tcp": [{"HostPort": "3306"}]}},
        "Mounts": [{"Type": "volume", "Name": "f00d" * 16, "Destination": "/var/lib/mysql", "RW": True},
                   {"Type": "bind", "Source": "/etc/db", "Destination": "/etc/mysql/conf.d", "RW": False}],
        "NetworkSettings": {"Networks": {"proxy": {"Aliases": ["db", "abcdef123456"]}, "backend": {"Aliases": None, "IPAMConfig": {"IPv4Address": "10.0.0.5"}}}},
    }  # fmt: skip
    image = {"Env": ["MYSQL_MAJOR=8.0"], "Cmd": ["mysqld"], "Entrypoint": ["docker-entrypoint.sh"], "WorkingDir": "/", "Labels": {"maintainer": "x"},
             "Volumes": {"/var/lib/mysql": {}}}  # fmt: skip
    config, extra = inplace.new_config(info, image, "mysql:8.4")
    assert config["Image"] == "mysql:8.4" and config["Env"] == ["MYSQL_ROOT_PASSWORD=x"]
    assert "Cmd" not in config and "Entrypoint" not in config and config["WorkingDir"] == "/srv"  # zmieniony przez kogoś — zostaje
    assert config["Labels"] == {"mine": "1"} and config["Volumes"] is None and "Hostname" not in config
    assert config["HostConfig"]["Binds"] == ["/etc/db:/etc/mysql/conf.d:ro", f"{'f00d' * 16}:/var/lib/mysql"]  # dane bazy zostają
    assert config["NetworkingConfig"] == {"EndpointsConfig": {"proxy": {"Aliases": ["db"]}}}
    assert extra == {"backend": {"IPAMConfig": {"IPv4Address": "10.0.0.5"}}}


def test_deployra_image_and_bad_health_path_are_refused(api, ctx):
    cid = make_container(ctx, image="ghcr.io/pans4ge/deployra-panel:1.8.0", name="panel")  # bez etykiety systemowej
    assert run(api, cid, image="nginx:1")["job"]["code"] == "DPL-SYSTEM"
    bad = api.request("POST", f"/api/containers/{cid}/update", json={"image": "nginx:1", "health_path": "@evil.example/"})
    assert bad.status_code == 422  # test zdrowia zawsze na 127.0.0.1:<port>/…, nie na cudzym hoście

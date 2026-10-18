import gzip

import pytest
from deployra_backend.services import docker_svc


def test_restore_dump_uploads_sql_and_runs_restore_command(ctx):
    fake = ctx.docker_for("local")
    cid = fake.api.create_container("postgres:16", name="pg-restore", environment={"POSTGRES_USER": "app", "POSTGRES_DB": "shop"})["Id"]
    fake.api.start(cid)

    res = docker_svc.restore_dump(fake, cid, gzip.compress(b"CREATE TABLE t();"))

    assert res["exit_code"] == 0
    uploaded = [path for path in fake.api._files if path.startswith("/tmp/deployra-restore-")]
    assert len(uploaded) == 1
    assert fake.api._files[uploaded[0]] == b"CREATE TABLE t();"  # w kontenerze leży rozpakowany zrzut


def test_restore_dump_refuses_stopped_container(ctx):
    fake = ctx.docker_for("local")
    cid = fake.api.create_container("postgres:16", name="pg-stopped", environment={"POSTGRES_USER": "app"})["Id"]

    with pytest.raises(ValueError, match="działającego kontenera"):
        docker_svc.restore_dump(fake, cid, gzip.compress(b"SELECT 1;"))


def test_restore_dump_rejects_bad_gzip(ctx):
    fake = ctx.docker_for("local")
    cid = fake.api.create_container("postgres:16", name="pg-bad", environment={"POSTGRES_USER": "app"})["Id"]
    fake.api.start(cid)

    with pytest.raises(ValueError, match="gzip"):
        docker_svc.restore_dump(fake, cid, b"to nie jest gzip")


def test_restore_dump_refuses_non_database(ctx):
    fake = ctx.docker_for("local")
    cid = fake.api.create_container("nginx:alpine", name="web-restore")["Id"]
    fake.api.start(cid)

    with pytest.raises(ValueError, match="nie jest obsługiwane"):
        docker_svc.restore_dump(fake, cid, gzip.compress(b"x"))

import pytest
from deployra_common import container_profiles as cp


def test_postgres_restore_resets_schema_then_imports_file():
    env = ["POSTGRES_USER=app", "POSTGRES_DB=my shop"]
    cmd = cp.build_restore_command(cp.detect_profile("postgres:16", env), env, "/tmp/x.sql")

    assert cmd[:2] == ["sh", "-c"] and cmd[-2:] == ["sh", "/tmp/x.sql"]
    script = cmd[2]
    assert "DROP SCHEMA IF EXISTS public CASCADE" in script
    assert "psql -U app -d 'my shop'" in script  # nazwa bazy z spacją jest cytowana
    assert script.index("DROP SCHEMA") < script.index('< "$1"')
    assert script.endswith('rc=$?; rm -f "$1"; exit $rc')  # plik znika także przy błędzie


def test_mysql_restore_uses_root_password_and_single_database():
    env = ["MYSQL_ROOT_PASSWORD=p@ss w", "MYSQL_DATABASE=shop"]
    cmd = cp.build_restore_command(cp.detect_profile("mysql:8", env), env, "/tmp/x.sql")

    script = cmd[2]
    assert "mysql -uroot" in script and "shop" in script
    assert "'-pp@ss w'" in script  # hasło cytowane, nie rozbija polecenia
    assert '< "$1"' in script


def test_mariadb_without_database_restores_whole_server():
    env = ["MARIADB_ROOT_PASSWORD=x"]
    script = cp.build_restore_command(cp.detect_profile("mariadb:11", env), env, "/tmp/x")[2]
    assert script.startswith("mysql -uroot -px")
    assert "shop" not in script


def test_mongodb_restore_drops_collections_before_import():
    cmd = cp.build_restore_command(cp.detect_profile("mongo:7"), [], "/tmp/x.archive")
    assert "mongorestore" in cmd[2] and "--archive=" in cmd[2] and "--gzip" in cmd[2] and "--drop" in cmd[2]


@pytest.mark.parametrize("image", ["nginx:alpine", "redis:7"])
def test_non_restorable_profiles_are_refused(image):
    with pytest.raises(ValueError):
        cp.build_restore_command(cp.detect_profile(image), [], "/tmp/x")

import json

import pytest

from deployra_backend.services import inventory


def deploy(api, spec):
    job = api.request("POST", "/api/apps/deploy", json=spec).json()["job"]
    return api.wait_job(job["id"])


@pytest.mark.parametrize("raw, product, expected", [
    ("8.3.12", "php", "8.3.12"),
    ("1:11.4.3+maria~ubu2404", "mariadb", "11.4.3"),
    ("16.4-1.pgdg120+2", "postgresql", "16.4"),
    ("jdk-21.0.4+7", "java", "21.0.4"),
    ("3.13", "python", None),  # bez numeru poprawki nie wiadomo, która to wersja
    ("", "php", None),
    ("latest", "php", None),
])  # fmt: skip
def test_clean_version(raw, product, expected):
    assert inventory.clean_version(raw, product) == expected


def image(env=(), labels=None, tag=""):
    return {"Config": {"Env": ["PATH=/usr/bin", *env], "Labels": labels or {}}, "RepoTags": [tag] if tag else []}


def names(software):
    return {(s["name"], s["version"]) for s in software}


def test_image_software_from_env_labels_and_tags():
    # WordPress stoi na obrazie PHP — oba się liczą (PHP2Shell dotyczy też WordPressa)
    assert names(inventory.image_software(image(["PHP_VERSION=8.2.23", "WORDPRESS_VERSION=6.6.2"]))) == {("php", "8.2.23"), ("wordpress", "6.6.2")}
    assert names(inventory.image_software(image(["PG_VERSION=16.4-1.pgdg120+2"]))) == {("postgresql", "16.4")}
    assert names(inventory.image_software(image(tag="grafana/grafana:11.2.0"))) == {("grafana", "11.2.0")}
    assert names(inventory.image_software(image(labels={"org.opencontainers.image.version": "1.22.3"}), "gitea/gitea:1")) == {("gitea", "1.22.3")}
    assert names(inventory.image_software(image(["BITNAMI_APP_NAME=php-fpm", "APP_VERSION=8.3.10"]), "bitnami/php-fpm:8.3")) == {("php", "8.3.10")}
    assert inventory.image_software(image(), "postgres:16-alpine") == []  # tag gałęzi, nie wersja
    assert inventory.image_software(image(labels={"org.opencontainers.image.version": "24.04"}), "my/app:1.0.0") == []  # nieznany projekt
    assert all(s["ecosystem"] == "Bitnami" for s in inventory.image_software(image(["NODE_VERSION=20.11.1"])))


def test_app_packages_from_lockfiles(tmp_path):
    (tmp_path / "composer.lock").write_text(json.dumps({"packages": [{"name": "laravel/framework", "version": "v10.0.0"}],
                                                        "packages-dev": [{"name": "phpunit/phpunit", "version": "10.0.0"}]}))  # fmt: skip
    (tmp_path / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": {
        "": {"name": "app"}, "node_modules/lodash": {"version": "4.17.20"}, "node_modules/a/node_modules/@scope/b": {"version": "1.0.0"},
        "node_modules/jest": {"version": "29.0.0", "dev": True}}}))  # fmt: skip
    (tmp_path / "requirements.txt").write_text("# komentarz\nDjango==4.2.0\nrequests>=2\nuvicorn[standard]==0.30.1 ; python_version>'3'\n-r other.txt\n")
    (tmp_path / "go.mod").write_text("module example.com/app\n\ngo 1.22\n\nrequire (\n\tgolang.org/x/net v0.17.0 // indirect\n\tgithub.com/gin-gonic/gin v1.9.1\n)\n"
                                     "require github.com/x/y v2.0.0+incompatible\n")  # fmt: skip
    (tmp_path / "Cargo.lock").write_text('[[package]]\nname = "app"\nversion = "0.1.0"\n\n[[package]]\nname = "tokio"\nversion = "1.20.0"\n'
                                         'source = "registry+https://github.com/rust-lang/crates.io-index"\n')  # fmt: skip
    (tmp_path / "Gemfile.lock").write_text("GEM\n  remote: https://rubygems.org/\n  specs:\n    rails (7.0.0)\n      actionpack (= 7.0.0)\n"
                                           "    nokogiri (1.15.4-x86_64-linux)\n\nPLATFORMS\n  ruby\n")  # fmt: skip
    web = tmp_path / "web"
    web.mkdir()
    (web / "yarn.lock").write_text('# yarn lockfile v1\n\n"@babel/core@^7.0.0", "@babel/core@^7.1.0":\n  version "7.22.0"\n\nleft-pad@^1.0.0:\n  version "1.3.0"\n')
    (web / "node_modules").mkdir()  # pomijane przy szukaniu plików lock
    found = {(p["ecosystem"], p["name"], p["version"]) for p in inventory.app_packages(tmp_path)}
    assert found == {
        ("Packagist", "laravel/framework", "10.0.0"), ("npm", "lodash", "4.17.20"), ("npm", "@scope/b", "1.0.0"), ("PyPI", "django", "4.2.0"),
        ("PyPI", "uvicorn", "0.30.1"), ("Go", "golang.org/x/net", "0.17.0"), ("Go", "github.com/gin-gonic/gin", "1.9.1"),
        ("Go", "github.com/x/y", "2.0.0"), ("crates.io", "tokio", "1.20.0"), ("RubyGems", "rails", "7.0.0"), ("RubyGems", "nokogiri", "1.15.4"),
        ("npm", "@babel/core", "7.22.0"), ("npm", "left-pad", "1.3.0"),
    }  # fmt: skip
    sources = {p["name"]: p["source"] for p in inventory.app_packages(tmp_path)}
    assert sources["left-pad"] == "web/yarn.lock" and sources["django"] == "requirements.txt"


def test_installed_packages_win_over_lockfile(tmp_path):
    (tmp_path / "composer.lock").write_text(json.dumps({"packages": [{"name": "monolog/monolog", "version": "2.0.0"}]}))
    (tmp_path / "vendor" / "composer").mkdir(parents=True)
    (tmp_path / "vendor" / "composer" / "installed.json").write_text(json.dumps({
        "packages": [{"name": "monolog/monolog", "version": "2.9.1"}, {"name": "phpunit/phpunit", "version": "10.0.0"}],
        "dev-package-names": ["phpunit/phpunit"]}))  # fmt: skip
    site = tmp_path / ".deployra" / "venv" / "lib" / "python3.13" / "site-packages"
    (site / "Flask-3.0.0.dist-info").mkdir(parents=True)
    (site / "typing_extensions-4.9.0.dist-info").mkdir()
    (tmp_path / "requirements.txt").write_text("flask==2.0.0\n")
    found = {(p["ecosystem"], p["name"], p["version"]) for p in inventory.app_packages(tmp_path)}
    assert found == {("Packagist", "monolog/monolog", "2.9.1"), ("PyPI", "flask", "3.0.0"), ("PyPI", "typing-extensions", "4.9.0")}


def test_broken_lockfile_is_skipped(tmp_path):
    (tmp_path / "package-lock.json").write_text("{nie json")
    (tmp_path / "Cargo.lock").write_text("[[package]\n")
    (tmp_path / "requirements.txt").write_text("rich==13.0.0\n")
    assert [p["name"] for p in inventory.app_packages(tmp_path)] == ["rich"]


def test_inventory_endpoint_lists_containers_and_app_dependencies(api, ctx):
    result = deploy(api, {"slug": "sklep", "source": {"kind": "runtime", "runtime": "php"}})
    assert result["job"]["status"] == "ok", result["lines"]
    (ctx.config.sites_dir / "sklep" / "composer.lock").write_text(json.dumps({"packages": [{"name": "guzzlehttp/guzzle", "version": "7.4.0"}]}))
    ctx.docker.api.create_container("redis:7-alpine", name="cache", host_config={})
    data = api.request("GET", "/api/security/inventory").json()
    containers = {c["name"]: c for c in data["endpoints"][0]["containers"]}
    assert data["endpoints"][0]["id"] == "local" and data["endpoints"][0]["error"] is None
    assert names(containers["app-sklep"]["software"]) == {("php", "8.3.12")} and containers["app-sklep"]["app"] == "sklep"
    assert names(containers["cache"]["software"]) == {("redis", "7.2.5")}
    assert not any(c["system"] == "install" for c in containers.values())  # jednorazowe kontenery instalacji pomijamy
    assert data["apps"]["sklep"] == [{"ecosystem": "Packagist", "name": "guzzlehttp/guzzle", "version": "7.4.0", "source": "composer.lock"}]


def test_refresh_image_rebuilds_runtime_base(api, ctx):
    spec = {"slug": "sklep", "source": {"kind": "runtime", "runtime": "php"}}
    assert deploy(api, spec)["job"]["status"] == "ok"
    built = list(ctx.docker.api.built)
    assert deploy(api, spec)["job"]["status"] == "ok"
    assert ctx.docker.api.built == built  # zwykłe wdrożenie nie buduje obrazu bazowego drugi raz
    assert deploy(api, {**spec, "refresh_image": True})["job"]["status"] == "ok"
    assert len(ctx.docker.api.built) == len(built) + 1
    containers = {c["name"]: c for c in api.request("GET", "/api/security/inventory").json()["endpoints"][0]["containers"]}
    assert names(containers["app-sklep"]["software"]) == {("php", "8.3.36")}


def test_git_build_pulls_base_only_on_refresh(monkeypatch, tmp_path):
    from deployra_backend.services import git_build

    commands = []
    monkeypatch.setattr(git_build, "run_streaming", lambda job, cmd, cwd=None, env=None, shown=None: commands.append(cmd))
    (tmp_path / "Dockerfile").write_text("FROM php:8.3-apache\n")
    git_build.build(object(), tmp_path, "Dockerfile", ".", "deployra/x:1")
    git_build.build(object(), tmp_path, "Dockerfile", ".", "deployra/x:2", pull=True)
    assert "--pull" not in commands[0] and "--pull" in commands[1]


def test_image_digests_for_update_checks(api, ctx):
    spec = {"slug": "sklep", "source": {"kind": "runtime", "runtime": "php"}}
    assert deploy(api, spec)["job"]["status"] == "ok"
    ctx.docker.api.create_container("redis:7-alpine", name="cache", host_config={})

    def containers():
        data = api.request("GET", "/api/images/digests").json()
        return {c["name"]: c for c in data["endpoints"][0]["containers"]}

    before = containers()
    php = ctx.docker.api.image_digest("php:8.3-apache")
    # aplikacja „z plików”: liczy się obraz języka, z którego zbudowano obraz bazowy (tu jeszcze bez etykiety)
    assert before["app-sklep"]["image"] == "php:8.3-apache" and before["app-sklep"]["base"] and before["app-sklep"]["digests"] == [php]
    assert before["cache"]["image"] == "redis:7-alpine" and before["cache"]["digests"] == [ctx.docker.api.image_digest("redis:7-alpine")]
    assert not any(c["name"].endswith("-install") for c in before.values())

    assert deploy(api, {**spec, "refresh_image": True})["job"]["status"] == "ok"
    after = containers()["app-sklep"]
    assert after["digests"] == [ctx.docker.api.image_digest("php:8.3-apache")] != [php]  # nowy obraz języka, digest w etykiecie

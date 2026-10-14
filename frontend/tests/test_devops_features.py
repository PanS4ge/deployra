import json
from pathlib import Path
import pytest

from deployra_panel.services import backup_storage, registries


def test_backup_storage_local_and_retention(admin, tmp_path):
    panel = admin.app.state.panel
    panel.db.set_setting("panel_domain", "deployra.test")

    # Uruchomienie backupu na dysk lokalny
    conf = {
        "target": "local",
        "local_path": str(tmp_path),
        "retention": 2,
        "secrets_included": True,
    }
    res1 = backup_storage.run_backup(panel, conf)
    assert res1["ok"] is True
    file1 = tmp_path / res1["filename"]
    assert file1.exists()

    payload = json.loads(file1.read_text(encoding="utf-8"))
    assert payload["deployra"] == 1
    assert "settings" in payload["tables"]

    # Drugi i trzeci backup w celu weryfikacji retencji (max 2)
    res2 = backup_storage.run_backup(panel, conf)
    res3 = backup_storage.run_backup(panel, conf)
    assert res2["ok"] is True and res3["ok"] is True

    files = list(tmp_path.glob("deployra-backup-*.json"))
    assert len(files) == 2


def test_backup_storage_driver_test(tmp_path):
    driver = backup_storage.LocalDriver(str(tmp_path))
    msg = driver.test()
    assert "dostępny" in msg


def test_registries_crud_and_auth_lookup(admin):
    panel = admin.app.state.panel

    # Dodanie rejestru prywatnego
    reg_id = registries.add_registry(panel, "My Harbor", "harbor.internal.net:5000", "devops_user", "very_secret_token")
    assert reg_id > 0

    items = registries.list_registries(panel)
    assert any(r["name"] == "My Harbor" and r["username"] == "devops_user" for r in items)

    # Hasło w bazie jest zapieczętowane, nie w plain text
    raw_row = panel.db.fetchone("SELECT * FROM registries WHERE id = ?", [reg_id])
    assert "very_secret_token" not in str(raw_row["password"])

    # Lookup auth dla obrazu z tego rejestru
    auth = registries.auth_for_image(panel, "harbor.internal.net:5000/myteam/backend:v2.1")
    assert auth is not None
    assert auth["username"] == "devops_user"
    assert auth["password"] == "very_secret_token"
    assert auth["serveraddress"] == "harbor.internal.net:5000"

    # Inny rejestr bez poświadczeń
    assert registries.auth_for_image(panel, "ghcr.io/org/repo:latest") is None
    assert registries.auth_for_image(panel, "ubuntu:latest") is None

    # Dodanie poświadczeń dla domyślnego Docker Hub
    hub_id = registries.add_registry(panel, "Docker Hub", "docker.io", "hubuser", "hubpass")
    hub_auth = registries.auth_for_image(panel, "redis:alpine")
    assert hub_auth is not None
    assert hub_auth["username"] == "hubuser"
    assert hub_auth["password"] == "hubpass"

    # Usunięcie rejestru
    registries.delete_registry(panel, reg_id)
    registries.delete_registry(panel, hub_id)
    assert registries.auth_for_image(panel, "harbor.internal.net:5000/myteam/backend:v2.1") is None


def test_backup_and_registries_http_routes(admin, tmp_path):
    # GET /settings/backup
    resp = admin.get("/settings/backup")
    assert resp.status_code == 200
    assert "backup" in resp.text

    # POST /settings/backup/storage
    resp = admin.post("/settings/backup/storage", data={
        "target": "local",
        "local_path": str(tmp_path),
        "retention": "5",
        "secrets_included": "1",
    })
    assert resp.status_code == 204
    assert resp.headers["HX-Redirect"] == "/settings/backup"

    # POST /settings/backup/storage/test
    resp = admin.post("/settings/backup/storage/test")
    assert resp.status_code == 200
    assert "toast" in resp.headers["HX-Trigger"]

    # POST /settings/backup/storage/run
    resp = admin.post("/settings/backup/storage/run")
    assert resp.status_code == 200
    assert "toast" in resp.headers["HX-Trigger"]

    # GET /settings/registries
    resp = admin.get("/settings/registries")
    assert resp.status_code == 200
    assert "registries" in resp.text

    # POST /settings/registries
    resp = admin.post("/settings/registries", data={
        "name": "GHCR",
        "url": "ghcr.io",
        "username": "gh-user",
        "password": "gh-token-123",
    })
    assert resp.status_code == 204
    assert resp.headers["HX-Redirect"] == "/settings/registries"

    # Verify created
    panel = admin.app.state.panel
    reg = panel.db.fetchone("SELECT * FROM registries WHERE name = 'GHCR'")
    assert reg is not None

    # POST /settings/registries/{id}/delete
    resp = admin.post(f"/settings/registries/{reg['id']}/delete")
    assert resp.status_code == 204
    assert resp.headers["HX-Redirect"] == "/settings/registries"
    assert panel.db.fetchone("SELECT * FROM registries WHERE id = ?", [reg["id"]]) is None

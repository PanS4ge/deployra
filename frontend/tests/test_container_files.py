import json
import pytest
from fastapi.testclient import TestClient

from deployra_panel.context import Panel
from deployra_panel.permissions import Permission
from deployra_panel.security import SESSION_COOKIE


class MockFilesNodeClient:
    def __init__(self):
        self.written = []
        self.deleted = []

    async def get(self, path, params=None, **_):
        if "/files/read" in path:
            return {"path": (params or {}).get("path", "/app/test.txt"), "content": "echo 'hello'", "is_binary": False, "size": 12}
        if "/files" in path:
            return {
                "path": (params or {}).get("path", "/"),
                "items": [
                    {"name": "app", "path": "/app", "is_dir": True, "size": 0},
                    {"name": "test.txt", "path": "/test.txt", "is_dir": False, "size": 12},
                ],
            }
        # container inspect
        return {"Id": "cid123456789", "Name": "/test-app", "State": {"Status": "running", "Running": True}}

    async def post(self, path, json=None, data=None, files=None, **_):
        self.written.append((path, json or data))
        return {"ok": True}

    async def delete(self, path, params=None, **_):
        self.deleted.append((path, params))
        return {"ok": True}


@pytest.fixture
def node_id(panel: Panel):
    return panel.db.insert("nodes", name="node1", transport="direct", api_url="http://127.0.0.1:1", status="paired")


def test_container_files_view_and_read(panel: Panel, client: TestClient, node_id: int, monkeypatch):
    mock_node = MockFilesNodeClient()
    monkeypatch.setattr(panel, "node_client", lambda n: mock_node)

    # Logowanie jako admin (permissions="0" -> FULL_ACCESS)
    admin_id = panel.db.insert("users", email="admin@deployra.local", password_hash="hash", name="Admin", permissions="0")
    sid, csrf = panel.sessions.create(admin_id, "127.0.0.1", "pytest")
    client.cookies.set(SESSION_COOKIE, sid)

    # 1. Wejście na stronę przeglądarki plików
    res = client.get(f"/nodes/{node_id}/containers/cid123/files")
    assert res.status_code == 200
    assert "Pliki" in res.text or "Files" in res.text
    assert "test-app" in res.text
    assert "test.txt" in res.text
    assert "app" in res.text

    # 2. HTMX żądanie do partiala
    res_partial = client.get(f"/nodes/{node_id}/containers/cid123/files?path=/app", headers={"HX-Request": "true"})
    assert res_partial.status_code == 200
    assert "files-table-container" in res_partial.text

    # 3. Odczyt zawartości pliku
    res_read = client.get(f"/nodes/{node_id}/containers/cid123/files/read?path=/app/test.txt")
    assert res_read.status_code == 200
    data = res_read.json()
    assert data["content"] == "echo 'hello'"
    assert data["is_binary"] is False

    # 4. Zapis pliku
    res_write = client.post(
        f"/nodes/{node_id}/containers/cid123/files/write",
        data={"path": "/app/test.txt", "content": "echo 'updated'"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res_write.status_code == 200
    assert any("/files/write" in call[0] for call in mock_node.written)

    # 5. Utworzenie katalogu (mkdir)
    res_mkdir = client.post(
        f"/nodes/{node_id}/containers/cid123/files/mkdir",
        data={"name": "newdir", "dir_path": "/app"},
        headers={"X-CSRF-Token": csrf},
        follow_redirects=False,
    )
    assert res_mkdir.status_code == 303
    assert any("/files/mkdir" in call[0] for call in mock_node.written)

    # 6. Usunięcie pliku
    res_del = client.post(
        f"/nodes/{node_id}/containers/cid123/files/delete",
        data={"path": "/app/test.txt", "dir_path": "/app"},
        headers={"X-CSRF-Token": csrf},
        follow_redirects=False,
    )
    assert res_del.status_code == 303
    assert any("/files" in call[0] for call in mock_node.deleted)


def test_container_files_permissions(panel: Panel, client: TestClient, node_id: int, monkeypatch):
    mock_node = MockFilesNodeClient()
    monkeypatch.setattr(panel, "node_client", lambda n: mock_node)

    # 1. Użytkownik bez żadnych uprawnień
    no_perm_user = panel.db.insert("users", email="noperms@deployra.local", password_hash="hash", name="NoPerm", permissions="")
    sid1, csrf1 = panel.sessions.create(no_perm_user, "127.0.0.1", "pytest")
    client.cookies.set(SESSION_COOKIE, sid1)

    res_no = client.get(f"/nodes/{node_id}/containers/cid123/files")
    assert res_no.status_code == 403

    # 2. Użytkownik TYLKO z uprawnieniem do odczytu (ALLOW_CONTAINER_FILES = 'P')
    panel.db.update("users", no_perm_user, permissions=Permission.ALLOW_CONTAINER_FILES.value)
    res_ro = client.get(f"/nodes/{node_id}/containers/cid123/files")
    assert res_ro.status_code == 200

    # Próba zapisu pliku bez uprawnienia ALLOW_CONTAINER_FILES_WRITE ('Q') -> 403
    res_ro_write = client.post(
        f"/nodes/{node_id}/containers/cid123/files/write",
        data={"path": "/app/test.txt", "content": "forbidden"},
        headers={"X-CSRF-Token": csrf1},
    )
    assert res_ro_write.status_code == 403

    # Nadanie uprawnienia ALLOW_CONTAINER_FILES_WRITE ('Q')
    panel.db.update(
        "users",
        no_perm_user,
        permissions=Permission.ALLOW_CONTAINER_FILES.value + Permission.ALLOW_CONTAINER_FILES_WRITE.value,
    )
    res_rw_write = client.post(
        f"/nodes/{node_id}/containers/cid123/files/write",
        data={"path": "/app/test.txt", "content": "allowed now"},
        headers={"X-CSRF-Token": csrf1},
    )
    assert res_rw_write.status_code == 200

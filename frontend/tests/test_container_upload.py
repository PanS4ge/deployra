import io
import tarfile

from deployra_panel.routes import docker as docker_routes


class FakeNode:
    def __init__(self):
        self.requests, self.uploads = [], []

    async def request(self, method, path, **kwargs):
        name, data, _ = kwargs["files"][0][1]
        self.requests.append((path, name, data))

    async def upload_file(self, path, file_path, timeout=None):
        with tarfile.open(file_path) as tar:
            self.uploads.append((path, {m.name: (tar.extractfile(m).read() if m.isfile() else None) for m in tar}))


def _setup(admin, panel, monkeypatch):
    node_id = panel.db.insert("nodes", name="mc", transport="direct", api_url="http://127.0.0.1:1", status="paired")
    fake = FakeNode()
    monkeypatch.setattr(docker_routes, "_client", lambda _panel, _node: fake)
    return f"/nodes/{node_id}/containers/abc/files/upload", fake


def test_folder_upload_goes_as_streamed_tar(admin, panel, monkeypatch):
    url, fake = _setup(admin, panel, monkeypatch)
    files = [("files", ("plugins/Essentials.jar", io.BytesIO(b"jar"), "application/java-archive")),
             ("files", ("plugins/Essentials/config.yml", io.BytesIO(b"a: 1"), "text/yaml"))]
    response = admin.post(url, data={"dir_path": "/data"}, files=files)
    assert response.status_code in (204, 303), response.text
    assert fake.requests == [] and len(fake.uploads) == 1
    path, entries = fake.uploads[0]
    assert path.startswith("/api/containers/abc/files/upload-tar?dir_path=/data") and path.endswith("ep=local")
    assert entries == {"plugins": None, "plugins/Essentials.jar": b"jar", "plugins/Essentials": None, "plugins/Essentials/config.yml": b"a: 1"}


def test_small_single_files_go_one_by_one(admin, panel, monkeypatch):
    url, fake = _setup(admin, panel, monkeypatch)
    files = [("files", ("a.txt", io.BytesIO(b"A"), "text/plain")), ("files", ("b.txt", io.BytesIO(b"B"), "text/plain"))]
    assert admin.post(url, data={"dir_path": "/srv"}, files=files).status_code in (204, 303)
    assert [(name, data) for _, name, data in fake.requests] == [("a.txt", b"A"), ("b.txt", b"B")] and fake.uploads == []


def test_parent_paths_are_rejected(admin, panel, monkeypatch):
    url, fake = _setup(admin, panel, monkeypatch)
    response = admin.post(url, data={"dir_path": "/data"}, files=[("files", ("x/../../etc/passwd", io.BytesIO(b"x"), "text/plain"))])
    assert response.status_code == 400 and fake.uploads == [] and fake.requests == []

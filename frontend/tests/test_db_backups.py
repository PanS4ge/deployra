"""Harmonogram kopii baz danych: cron, retencja, magazyn, zapis harmonogramów, zrzut i przywracanie.

Węzeł zastępuje atrapa (`FakeNode`), magazyn to katalog lokalny w tmp_path.
"""

import asyncio
import json
import re
from datetime import datetime

import pytest

from deployra_common.client import RemoteError
from deployra_panel.db import now
from deployra_panel.services import backup_storage, db_backups

PG = {"kind": "postgres", "category": "database", "title": "PostgreSQL"}


class FakeResponse:
    def __init__(self, content=b"", headers=None, payload=None):
        self.content = content
        self.headers = headers or {}
        self._payload = payload

    def json(self):
        return self._payload


class FakeNode:
    """Klient węzła: `get` zwraca profil kontenera, `request` zrzut albo wynik przywracania."""

    def __init__(self, profile=PG, dump=b"GZ", fail=None, restore_code=0):
        self.profile, self.dump, self.fail, self.restore_code = profile, dump, fail, restore_code
        self.calls = []

    async def get(self, path, **kwargs):
        self.calls.append(("GET", path, None))
        return self.profile

    async def request(self, method, path, timeout=None, **kwargs):
        self.calls.append((method, path, kwargs.get("content")))
        if self.fail:
            raise self.fail
        if method == "GET":
            return FakeResponse(content=self.dump, headers={"content-disposition": 'attachment; filename="pg_app.sql.gz"'})
        return FakeResponse(content=b"{}", payload={"exit_code": self.restore_code, "output": "ok"})


def add_node(panel, name="node-a"):
    return panel.db.insert("nodes", name=name, transport="http", status="paired", created_at=now())


def use_local_storage(panel, path):
    panel.db.set_setting("backup_config", {"target": "local", "local_path": str(path), "retention": 10})


def at(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M")


def toast_of(response):
    return json.loads(response.headers["HX-Trigger"])["toast"]["message"]  # nagłówki JSON są escapowane (ó)


# --- cron -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr, when, expected",
    [
        ("*/15 * * * *", "2026-10-18 10:30", True),
        ("*/15 * * * *", "2026-10-18 10:31", False),
        ("0 3 * * *", "2026-10-18 03:00", True),
        ("0 3 * * *", "2026-10-18 04:00", False),
        ("1-10/3 * * * *", "2026-10-18 10:07", True),  # 1, 4, 7, 10
        ("1-10/3 * * * *", "2026-10-18 10:08", False),
        ("5/20 * * * *", "2026-10-18 10:25", True),  # od 5 co 20
        ("0 9 * * 1-5", "2026-10-19 09:00", True),  # poniedziałek
        ("0 9 * * 1-5", "2026-10-18 09:00", False),  # niedziela
        ("0 0 * * 7", "2026-10-18 00:00", True),  # 7 = niedziela
        ("0 0 * * 0", "2026-10-18 00:00", True),
        ("0 0 13 * 5", "2026-10-13 00:00", True),  # dzień miesiąca (13.) — wystarczy jeden warunek
        ("0 0 13 * 5", "2026-10-16 00:00", True),  # piątek
        ("0 0 13 * 5", "2026-10-17 00:00", False),  # sobota, nie 13.
        ("0 0 1 1 *", "2027-01-01 00:00", True),
        ("@daily", "2026-10-18 00:00", True),
        ("@daily", "2026-10-18 00:01", False),
        ("@hourly", "2026-10-18 17:00", True),
        ("@hourly", "2026-10-18 17:01", False),
        ("@weekly", "2026-10-18 00:00", True),
        ("@weekly", "2026-10-19 00:00", False),
    ],
)
def test_cron_matches(expr, when, expected):
    assert db_backups.cron_matches(expr, at(when)) is expected


@pytest.mark.parametrize(
    "expr",
    ["60 * * * *", "* * * *", "*/0 * * * *", "a * * * *", "5-1 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *", "* * * * 8", "1,,2 * * * *", "", "@sometimes"],
)
def test_cron_rejects_invalid(expr):
    with pytest.raises(db_backups.CronError):
        db_backups.parse_cron(expr)


# --- magazyn i retencja ---------------------------------------------------------------------------


def test_local_driver_nested_keys(tmp_path):
    driver = backup_storage.LocalDriver(str(tmp_path / "magazyn"))
    driver.put("db/node-a/app-db/x.sql.gz", b"123")
    assert driver.get("db/node-a/app-db/x.sql.gz") == b"123"
    assert driver.list_files("db/node-a/app-db") == [{"name": "x.sql.gz", "size": 3}]
    assert driver.list_files("db/none") == []
    driver.delete("db/node-a/app-db/x.sql.gz")
    assert driver.list_files("db/node-a/app-db") == []


def test_retention_keeps_newest_dumps_of_one_schedule(tmp_path):
    driver = backup_storage.LocalDriver(str(tmp_path))
    folder = "db/node-a/app-db"
    for stamp in ["20261001-030000", "20261002-030000", "20261003-030000", "20261004-030000"]:
        driver.put(f"{folder}/{db_backups.dump_file_name(1, stamp, 'dump_sql')}", b"x")
    driver.put(f"{folder}/{db_backups.dump_file_name(2, '20260101-000000', 'dump_sql')}", b"y")
    driver.put(f"{folder}/notatka.txt", b"z")

    assert db_backups.apply_retention(driver, folder, 1, keep=2) == 2

    assert sorted(f["name"] for f in driver.list_files(folder)) == [
        "notatka.txt",
        "s1_20261003-030000.sql.gz",
        "s1_20261004-030000.sql.gz",
        "s2_20260101-000000.sql.gz",
    ]


def test_dumps_of_lists_newest_first_and_ignores_other_files():
    files = [
        {"name": "s1_20261001-030000.sql.gz", "size": 1},
        {"name": "s1_20261003-030000.archive.gz", "size": 3},
        {"name": "s12_20261005-030000.sql.gz", "size": 5},
        {"name": "s1_bad.sql.gz", "size": 9},
    ]
    assert [d["name"] for d in db_backups.dumps_of(files, 1)] == ["s1_20261003-030000.archive.gz", "s1_20261001-030000.sql.gz"]


# --- harmonogramy (zapis) -------------------------------------------------------------------------


def test_schedule_crud(panel, tmp_path):
    node = add_node(panel)
    sid = db_backups.create(panel, node, "app-db", "0 3 * * *", 5)

    row = db_backups.get(panel, sid)
    assert row["folder"] == "db/node-a/app-db" and row["node_name"] == "node-a"
    assert row["cron"] == "0 3 * * *" and row["retention"] == 5 and row["enabled"] == 1

    db_backups.update(panel, sid, "@daily", 3, False)
    row = db_backups.get(panel, sid)
    assert row["cron"] == "@daily" and row["retention"] == 3 and row["enabled"] == 0

    assert len(db_backups.schedules(panel)) == 1
    db_backups.delete(panel, sid)
    assert db_backups.schedules(panel) == []
    with pytest.raises(ValueError):
        db_backups.get(panel, sid)


def test_schedule_validation(panel):
    node = add_node(panel)
    with pytest.raises(ValueError):
        db_backups.create(panel, node, "zła nazwa;", "0 3 * * *", 5)
    with pytest.raises(ValueError):
        db_backups.create(panel, node, "app-db", "99 * * * *", 5)
    with pytest.raises(ValueError):
        db_backups.create(panel, node, "app-db", "0 3 * * *", 0)
    with pytest.raises(ValueError):
        db_backups.create(panel, node + 99, "app-db", "0 3 * * *", 5)


# --- zrzut, przywracanie, harmonogram -------------------------------------------------------------


@pytest.mark.anyio
async def test_run_stores_dump_and_records_success(panel, monkeypatch, tmp_path):
    use_local_storage(panel, tmp_path / "magazyn")
    sid = db_backups.create(panel, add_node(panel), "app-db", "0 3 * * *", 5)
    fake = FakeNode(dump=b"zrzut-gzip")
    monkeypatch.setattr(panel, "node_client", lambda node: fake)

    assert await db_backups.run(panel, sid) is True

    row = db_backups.get(panel, sid)
    assert row["last_status"] == "ok" and row["last_size"] == len(b"zrzut-gzip")
    assert re.fullmatch(rf"s{sid}_\d{{8}}-\d{{6}}\.sql\.gz", row["last_filename"])
    stored = tmp_path / "magazyn" / "db" / "node-a" / "app-db" / row["last_filename"]
    assert stored.read_bytes() == b"zrzut-gzip"
    assert ("GET", "/api/containers/app-db/action/quick/dump_sql?ep=local", None) in fake.calls


@pytest.mark.anyio
async def test_run_uses_archive_action_for_mongodb(panel, monkeypatch, tmp_path):
    use_local_storage(panel, tmp_path / "magazyn")
    sid = db_backups.create(panel, add_node(panel), "mongo", "0 3 * * *", 5)
    fake = FakeNode(profile={"kind": "mongodb", "category": "database", "title": "MongoDB"})
    monkeypatch.setattr(panel, "node_client", lambda node: fake)

    assert await db_backups.run(panel, sid) is True
    assert db_backups.get(panel, sid)["last_filename"].endswith(".archive.gz")
    assert any(path.endswith("/action/quick/dump_archive?ep=local") for _, path, _ in fake.calls)


@pytest.mark.anyio
async def test_run_failure_is_recorded_and_notified(panel, monkeypatch, tmp_path):
    use_local_storage(panel, tmp_path / "magazyn")
    sid = db_backups.create(panel, add_node(panel), "app-db", "0 3 * * *", 5)
    monkeypatch.setattr(panel, "node_client", lambda node: FakeNode(fail=RemoteError(500, "pg_dump: brak połączenia")))

    assert await db_backups.run(panel, sid) is False

    row = db_backups.get(panel, sid)
    assert row["last_status"] == "error" and row["last_message"] == "pg_dump: brak połączenia"
    notes = panel.db.fetchall("SELECT message, level FROM notifications")
    assert any("app-db" in n["message"] and n["level"] == "err" for n in notes)


@pytest.mark.anyio
async def test_run_refuses_container_that_is_not_database(panel, monkeypatch, tmp_path):
    use_local_storage(panel, tmp_path / "magazyn")
    sid = db_backups.create(panel, add_node(panel), "web", "0 3 * * *", 5)
    monkeypatch.setattr(panel, "node_client", lambda node: FakeNode(profile={"kind": "nginx", "category": "webserver", "title": "Nginx"}))

    assert await db_backups.run(panel, sid) is False
    assert db_backups.get(panel, sid)["last_message"] == "Kontener nie jest bazą danych"


@pytest.mark.anyio
async def test_restore_sends_stored_dump_back_to_node(panel, monkeypatch, tmp_path):
    use_local_storage(panel, tmp_path / "magazyn")
    sid = db_backups.create(panel, add_node(panel), "app-db", "0 3 * * *", 5)
    name = db_backups.dump_file_name(sid, "20261018-030000", "dump_sql")
    backup_storage.LocalDriver(str(tmp_path / "magazyn")).put(f"db/node-a/app-db/{name}", b"GZ-DUMP")
    fake = FakeNode()
    monkeypatch.setattr(panel, "node_client", lambda node: fake)

    assert await db_backups.restore(panel, sid, name) == "app-db"

    method, path, content = fake.calls[-1]
    assert (method, path, content) == ("POST", "/api/containers/app-db/action/restore?ep=local", b"GZ-DUMP")
    with pytest.raises(ValueError):
        await db_backups.restore(panel, sid, "../../etc/passwd")


@pytest.mark.anyio
async def test_restore_reports_failed_exit_code(panel, monkeypatch, tmp_path):
    use_local_storage(panel, tmp_path / "magazyn")
    sid = db_backups.create(panel, add_node(panel), "app-db", "0 3 * * *", 5)
    name = db_backups.dump_file_name(sid, "20261018-030000", "dump_sql")
    backup_storage.LocalDriver(str(tmp_path / "magazyn")).put(f"db/node-a/app-db/{name}", b"GZ")
    monkeypatch.setattr(panel, "node_client", lambda node: FakeNode(restore_code=1))

    with pytest.raises(ValueError, match="kod 1"):
        await db_backups.restore(panel, sid, name)


@pytest.mark.anyio
async def test_tick_starts_due_schedules_once_per_minute(panel, monkeypatch):
    node = add_node(panel)
    due = db_backups.create(panel, node, "app-db", "30 3 * * *", 5)
    off = db_backups.create(panel, node, "other-db", "30 3 * * *", 5)
    db_backups.update(panel, off, "30 3 * * *", 5, False)
    started = []
    monkeypatch.setattr(db_backups, "start", lambda panel, sid: started.append(sid) or True)
    db_backups.LAST_FIRED.clear()

    assert await db_backups.tick(panel, at("2026-10-18 03:30")) == [due]
    assert await db_backups.tick(panel, at("2026-10-18 03:30")) == []  # ta sama minuta: bez dublowania
    assert await db_backups.tick(panel, at("2026-10-18 03:31")) == []
    assert await db_backups.tick(panel, at("2026-10-19 03:30")) == [due]
    assert started == [due, due]


@pytest.mark.anyio
async def test_start_skips_when_previous_dump_still_runs(panel):
    sid = db_backups.create(panel, add_node(panel), "app-db", "0 3 * * *", 5)

    async def slow():
        await asyncio.sleep(60)

    task = asyncio.ensure_future(slow())
    db_backups.RUNNING[sid] = task
    try:
        assert db_backups.start(panel, sid) is False
    finally:
        task.cancel()
        db_backups.RUNNING.pop(sid, None)


# --- trasy ----------------------------------------------------------------------------------------


def test_backup_page_has_database_section(admin):
    page = admin.get("/settings/backup").text
    assert "Kopie baz danych" in page and 'hx-get="/settings/backup/db/picker"' in page


def test_picker_without_running_databases(admin):
    assert "Brak uruchomionych baz danych" in admin.get("/settings/backup/db/picker").text


def test_schedule_routes(admin, panel, tmp_path, monkeypatch):
    node = add_node(panel)
    use_local_storage(panel, tmp_path / "magazyn")

    created = admin.post("/settings/backup/db", data={"target": f"{node}:app-db", "cron": "0 3 * * *", "retention": "4", "enabled": "1"})
    assert created.status_code == 204 and created.headers["HX-Redirect"] == "/settings/backup"
    sid = panel.db.fetchone("SELECT id FROM db_backup_schedules")["id"]

    bad = admin.post("/settings/backup/db", data={"target": f"{node}:app-db", "cron": "nie-cron", "retention": "4"})
    assert bad.status_code == 400

    assert admin.post(f"/settings/backup/db/{sid}", data={"cron": "@daily", "retention": "2"}).status_code == 204
    row = db_backups.get(panel, sid)
    assert row["cron"] == "@daily" and row["retention"] == 2 and row["enabled"] == 0  # odznaczone pole nie przychodzi

    name = db_backups.dump_file_name(sid, "20261018-030000", "dump_sql")
    backup_storage.LocalDriver(str(tmp_path / "magazyn")).put(f"db/node-a/app-db/{name}", b"GZ-DUMP")
    files = admin.get(f"/settings/backup/db/{sid}/files").text
    assert name in files and "Przywróć" in files

    download = admin.get(f"/settings/backup/db/{sid}/download/{name}")
    assert download.status_code == 200 and download.content == b"GZ-DUMP"
    assert admin.get(f"/settings/backup/db/{sid}/download/evil.sql.gz").status_code == 400

    monkeypatch.setattr(panel, "node_client", lambda n: FakeNode())
    restored = admin.post(f"/settings/backup/db/{sid}/restore", data={"name": name})
    assert toast_of(restored) == "Kopia przywrócona do app-db"

    monkeypatch.setattr(db_backups, "start", lambda panel, sid: True)
    ran = admin.post(f"/settings/backup/db/{sid}/run")
    assert toast_of(ran) == "Zrzut uruchomiony w tle"

    assert admin.post(f"/settings/backup/db/{sid}/delete").status_code == 204
    assert db_backups.schedules(panel) == []

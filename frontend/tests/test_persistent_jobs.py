import asyncio
import time
import pytest
from deployra_panel.db import Database
from deployra_panel.services.persistent_jobs import PersistentJobRegistry


@pytest.fixture
def db(tmp_path):
    d = Database(f"sqlite:///{tmp_path / 'test.db'}")
    d.migrate()
    return d


def test_persistent_jobs_save_and_reload(db):
    async def _test():
        reg = PersistentJobRegistry(db)
        
        # Start and finish a job
        def work(job):
            job.log("Starting job")
            job.log("Step 1 done")
            return {"status": "ok", "items": 5}

        job = reg.start("test_kind", "Test Job Title", work)
        await asyncio.sleep(0.1)

        assert job.status == "ok"
        assert job.result == {"status": "ok", "items": 5}
        assert len(job.lines) == 2

        # Verify reloading in a new registry instance (simulating restart)
        reg2 = PersistentJobRegistry(db)
        loaded = reg2.get(job.id)
        assert loaded is not None
        assert loaded.id == job.id
        assert loaded.title == "Test Job Title"
        assert loaded.status == "ok"
        assert loaded.result == {"status": "ok", "items": 5}
        assert loaded.lines == ["Starting job", "Step 1 done"]

    asyncio.run(_test())


def test_interrupted_jobs_on_restart(db):
    # Insert a job directly into DB with status 'running' to simulate power loss / crash
    db.execute(
        """INSERT INTO jobs (id, kind, title, status, error, result, lines, created, finished)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ["interrupted123", "update", "Aktualizacja do 1.2.0", "running", None, None, '["Pobieranie..."]', time.time(), None]
    )

    reg = PersistentJobRegistry(db)
    job = reg.get("interrupted123")
    assert job is not None
    assert job.status == "error"
    assert "Przerwane przez restart panelu" in job.error
    assert any("Przerwane" in line for line in job.lines)

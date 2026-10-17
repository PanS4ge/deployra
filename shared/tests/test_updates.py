import time

import pytest

from deployra_common.updates import UpdateAgent, is_newer, normalize, parse_version, same_version


def test_version_ordering():
    assert is_newer("1.2.0", "1.1.9") and is_newer("v1.10.0", "1.9.9")
    assert is_newer("1.2.0", "1.2.0-rc1") and not is_newer("1.2.0-rc1", "1.2.0")
    assert not is_newer("1.2.0", "1.2.0") and same_version("v1.2.0", "1.2.0")
    assert not is_newer("1.2.0", "śmieci") and parse_version("1.2") is None
    assert normalize("v1.2.3") == "1.2.3"
    with pytest.raises(ValueError):
        normalize("1.2.3; rm -rf /")


def _beat(directory, age=0, mode="image"):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "agent").write_text(f"mode={mode}\nupdated={int(time.time() - age)}\n", encoding="utf-8")


def test_agent_sidecar_flow(tmp_path):
    agent = UpdateAgent(tmp_path / "upd")
    assert agent.info() == {"available": False, "mode": None, "state": "idle", "target": None, "step": None, "message": "", "updated": None, "container": None}
    with pytest.raises(LookupError, match="brak aktualizatora"):
        agent.request("1.2.0")

    _beat(tmp_path / "upd", age=600)  # martwy znacznik życia
    assert not agent.info()["available"]
    _beat(tmp_path / "upd", mode="source")
    assert agent.info()["mode"] == "source"

    info = agent.request("v1.2.0")
    assert info["state"] == "queued" and info["target"] == "1.2.0" and info["step"] == "queued"
    assert "version=1.2.0" in (tmp_path / "upd" / "request").read_text(encoding="utf-8")
    with pytest.raises(LookupError, match="już trwa"):
        agent.request("1.2.0")

    (tmp_path / "upd" / "status").write_text("state=error\nversion=1.2.0\nmessage=pobieranie nie powiodło się\nupdated=1\n", encoding="utf-8")
    (tmp_path / "upd" / "log").write_text("a\nb\nc\n", encoding="utf-8")
    info = agent.info(log_lines=2)
    assert info["state"] == "error" and info["message"] == "pobieranie nie powiodło się" and info["log"] == ["b", "c"]
    agent.request("1.2.0")  # po błędzie można ponowić


def test_agent_native_launcher(tmp_path):
    launched = []
    agent = UpdateAgent(tmp_path / "upd", launcher=launched.append, native_ready=True)
    assert agent.info()["mode"] == "native"
    agent.request("1.3.0")
    assert launched == ["1.3.0"] and not (tmp_path / "upd" / "request").exists()

    def broken(version):
        raise LookupError("systemd-run: brak uprawnień")

    agent = UpdateAgent(tmp_path / "upd2", launcher=broken, native_ready=True)
    with pytest.raises(LookupError):
        agent.request("1.3.0")
    assert agent.info()["state"] == "error"  # nie zostaje „queued” na zawsze
    assert UpdateAgent(tmp_path / "x", launcher=broken, native_ready=False).info()["available"] is False

import subprocess
from datetime import datetime
from types import SimpleNamespace

import pytest

from hermeshands_device import checkup, executor


def _quiet_probes(monkeypatch):
    monkeypatch.setattr(
        checkup.psutil,
        "disk_usage",
        lambda path: SimpleNamespace(total=1000, used=800, free=200),
    )
    monkeypatch.setattr(
        checkup.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(total=1000, available=300, percent=70),
    )
    monkeypatch.setattr(checkup.psutil, "process_iter", lambda attrs=None: iter(()))
    monkeypatch.setattr(checkup.psutil, "net_if_stats", lambda: {})
    monkeypatch.setattr(checkup.psutil, "boot_time", lambda: 1_000_000_000)
    monkeypatch.setattr(checkup.psutil, "sensors_battery", lambda: None)
    monkeypatch.setattr(checkup.socket, "getaddrinfo", lambda *args, **kwargs: [object()])

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(checkup.socket, "create_connection", lambda *args, **kwargs: Connection())


def _facts(result):
    return {fact["id"]: fact for fact in result["facts"]}


def test_checkup_uses_the_frozen_v1_fact_schema(monkeypatch):
    _quiet_probes(monkeypatch)

    result = checkup.collect()

    assert set(result) == {"schema_version", "observed_at", "platform", "facts", "inferences"}
    assert result["schema_version"] == 1
    assert result["platform"]
    datetime.fromisoformat(result["observed_at"].replace("Z", "+00:00"))
    assert isinstance(result["facts"], list)
    assert isinstance(result["inferences"], list)
    for fact in result["facts"]:
        assert set(fact) == {
            "id",
            "label",
            "value",
            "unit",
            "status",
            "availability",
            "source",
        }
        assert isinstance(fact["id"], str)
        assert isinstance(fact["label"], str)
        assert fact["value"] is None or type(fact["value"]) in (str, int, float, bool)
        assert fact["unit"] is None or isinstance(fact["unit"], str)
        assert fact["status"] in {"ok", "warn", "bad", "unknown"}
        assert fact["availability"] in {"observed", "unsupported", "error"}
        assert isinstance(fact["source"], str)


@pytest.mark.parametrize(
    ("free", "used_memory", "disk_status", "memory_status"),
    [
        (20, 79, "warn", "ok"),
        (10, 80, "bad", "warn"),
        (21, 95, "ok", "bad"),
    ],
)
def test_disk_and_memory_facts_use_fixed_thresholds(
    monkeypatch, free, used_memory, disk_status, memory_status
):
    _quiet_probes(monkeypatch)
    monkeypatch.setattr(
        checkup.psutil,
        "disk_usage",
        lambda path: SimpleNamespace(total=100, used=100 - free, free=free),
    )
    monkeypatch.setattr(
        checkup.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(total=100, available=100 - used_memory, percent=used_memory),
    )

    facts = _facts(checkup.collect())

    assert facts["disk_free_percent"]["status"] == disk_status
    assert facts["memory_used_percent"]["status"] == memory_status


def test_checkup_keeps_unsupported_and_failed_facts_explicit(monkeypatch):
    _quiet_probes(monkeypatch)
    monkeypatch.setattr(
        checkup.psutil,
        "disk_usage",
        lambda path: (_ for _ in ()).throw(PermissionError("inaccessible")),
    )
    monkeypatch.setattr(checkup.psutil, "sensors_battery", lambda: None)

    facts = _facts(checkup.collect())

    assert facts["disk_free_percent"]["value"] is None
    assert facts["disk_free_percent"]["status"] == "unknown"
    assert facts["disk_free_percent"]["availability"] == "error"
    assert facts["battery_percent"]["value"] is None
    assert facts["battery_percent"]["status"] == "unknown"
    assert facts["battery_percent"]["availability"] == "unsupported"


def test_process_facts_never_request_or_read_command_lines(monkeypatch):
    _quiet_probes(monkeypatch)
    requested = []

    class Process:
        info = {
            "name": "safe-process.exe",
            "memory_info": SimpleNamespace(rss=1024 * 1024),
        }

        @property
        def cmdline(self):
            raise AssertionError("process command lines must not be read")

    def process_iter(attrs=None):
        requested.extend(attrs or [])
        yield Process()

    monkeypatch.setattr(checkup.psutil, "process_iter", process_iter)

    facts = _facts(checkup.collect())

    assert "cmdline" not in requested
    assert facts["top_process_1"]["value"].startswith("safe-process.exe:")


def test_folder_size_scan_stops_at_its_entry_limit(monkeypatch, tmp_path):
    _quiet_probes(monkeypatch)
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    for index in range(10):
        (downloads / f"file-{index}.bin").write_bytes(b"x")
    monkeypatch.setattr(checkup.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(checkup, "MAX_FOLDER_ENTRIES", 3)
    monkeypatch.setattr(checkup, "MAX_FOLDER_SECONDS", 5)

    fact = _facts(checkup.collect())["folder_downloads_bytes"]

    assert 0 < fact["value"] <= 3
    assert fact["status"] == "warn"
    assert fact["availability"] == "observed"
    assert "entry limit" in fact["source"]


def test_checkup_folder_paths_cannot_inject_shell_commands(monkeypatch, tmp_path):
    _quiet_probes(monkeypatch)
    marker = tmp_path / "shell-ran"
    injected_temp = tmp_path / f"temp; touch {marker.name}"
    injected_temp.mkdir()
    monkeypatch.setenv("TEMP", str(injected_temp))
    monkeypatch.setattr(executor, "run", lambda *args, **kwargs: pytest.fail("executor shell used"))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("subprocess shell used"))
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("subprocess shell used"))
    monkeypatch.setattr(subprocess, "call", lambda *args, **kwargs: pytest.fail("subprocess shell used"))
    monkeypatch.setattr(checkup.os, "system", lambda *args, **kwargs: pytest.fail("OS shell used"))
    monkeypatch.setattr(checkup.os, "popen", lambda *args, **kwargs: pytest.fail("OS shell used"))

    result = checkup.collect()

    assert _facts(result)["folder_temp_bytes"]["availability"] == "observed"
    assert not marker.exists()

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import psutil

from hermeshands_device import executor


def test_windowsapps_which_candidate_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(executor.os, "name", "nt")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "none"))
    monkeypatch.delenv("HERMES_HANDS_BASH", raising=False)
    monkeypatch.setattr(
        shutil, "which", lambda name: r"C:\Users\x\AppData\Local\Microsoft\WindowsApps\bash.exe"
    )
    monkeypatch.setattr(os.path, "isfile", lambda path: True)
    assert executor.resolve_bash() is None


def test_resolve_bash_probes_each_candidate_once(monkeypatch, tmp_path):
    bash = tmp_path / "bash.exe"
    bash.write_bytes(b"")
    monkeypatch.setattr(executor, "_PROBED", set())
    monkeypatch.setattr(executor.os, "name", "nt")
    monkeypatch.setenv("HERMES_HANDS_BASH", str(bash))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "none"))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    probes = []
    monkeypatch.setattr(
        executor.subprocess, "run", lambda args, **kwargs: probes.append(args) or subprocess.CompletedProcess(args, 0)
    )
    assert executor.resolve_bash() == executor.resolve_bash() == str(bash)
    assert probes == [[str(bash), "-c", "exit 0"]]
    bash.unlink()
    assert executor.resolve_bash() is None  # a bash that went away is still noticed


def test_real_bash_execution(monkeypatch, tmp_path):
    bash = executor.resolve_bash()
    if not bash:
        pytest.skip("bash is unavailable")
    monkeypatch.setattr(executor, "resolve_bash", lambda: bash)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    chunks = b"".join(executor.run("echo hi", timeout=5))
    assert chunks.strip() == b"hi"


@pytest.mark.skipif(os.name != "nt", reason="Windows background-job pipe inheritance regression")
def test_background_process_survives_shell_exit(monkeypatch, tmp_path):
    bash = executor.resolve_bash()
    if not bash:
        pytest.skip("Git Bash is unavailable")
    monkeypatch.setattr(executor, "resolve_bash", lambda: bash)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    pid_file = tmp_path / "background.pid"
    script_file = tmp_path / "background.py"
    script_file.write_text(
        f"import os, time\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    started = time.monotonic()
    output = b"".join(
        executor.run(
            f'nohup python "{script_file.as_posix()}" >/dev/null 2>&1 & echo $!', timeout=5
        )
    )
    elapsed = time.monotonic() - started
    assert output.splitlines()[0].isdigit()
    deadline = time.monotonic() + 1
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    child_pid = int(pid_file.read_text(encoding="utf-8-sig"))
    try:
        assert elapsed < 1.0
        assert psutil.pid_exists(child_pid)
    finally:
        if psutil.pid_exists(child_pid):
            psutil.Process(child_pid).kill()


def test_large_script_runs_from_temp_file_and_is_removed(monkeypatch, tmp_path):
    bash = executor.resolve_bash()
    if not bash:
        pytest.skip("bash is unavailable")
    monkeypatch.setattr(executor, "resolve_bash", lambda: bash)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    script = "printf x\n" * 100_000
    assert b"".join(executor.run(script, timeout=10)) == b"x" * 100_000
    assert not list((tmp_path / ".hermes-hands/out").glob("*.sh"))


def test_timeout_kills_process(monkeypatch, tmp_path):
    monkeypatch.setattr(executor, "resolve_bash", lambda: shutil.which("bash"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    started = time.monotonic()
    chunks = b"".join(executor.run("sleep 10", timeout=0.1))
    assert b"timeout" in chunks
    assert time.monotonic() - started < 3


@pytest.mark.skipif(os.name == "nt", reason="POSIX process group is covered by timeout test")
def test_grandchild_killed_on_posix(monkeypatch, tmp_path):
    monkeypatch.setattr(executor, "resolve_bash", lambda: shutil.which("bash"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    chunks = b"".join(executor.run("sleep 30 & echo $!; wait", timeout=0.2))
    child_pid = int(chunks.splitlines()[0])
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)  # windows-footgun: ok -- POSIX-only test for killpg behavior


@pytest.mark.skipif(os.name != "nt", reason="Windows taskkill tree behavior")
def test_grandchild_killed_on_windows(monkeypatch, tmp_path):
    bash = executor.resolve_bash()
    if not bash:
        pytest.skip("Git Bash is unavailable")
    monkeypatch.setattr(executor, "resolve_bash", lambda: bash)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    bash_python = subprocess.run(
        [bash, "-c", "command -v python"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
    )
    if bash_python.returncode:
        pytest.skip("Python is unavailable inside Git Bash")
    script_file = tmp_path / "spawn-grandchild.py"
    script_file.write_text(
        "import subprocess, sys, time\nchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\nprint(child.pid, flush=True)\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    chunks = b"".join(executor.run(f'python "{script_file.as_posix()}"', timeout=2))
    child_pid = int(chunks.splitlines()[0])
    deadline = time.monotonic() + 2
    while psutil.pid_exists(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not psutil.pid_exists(child_pid)


def test_output_cap(monkeypatch, tmp_path):
    bash = executor.resolve_bash()
    if not bash:
        pytest.skip("bash is unavailable")
    monkeypatch.setattr(executor, "resolve_bash", lambda: bash)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(executor, "MAX_OUTPUT", 5)
    output = b"".join(executor.run("printf 123456789", timeout=5))
    assert output.startswith(b"12345")
    assert b"truncated" in output
    assert (tmp_path / ".hermes-hands/out/job.log").read_bytes() == b"12345"

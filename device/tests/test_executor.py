import os
import base64
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
    monkeypatch.setattr(executor, "_PROBED", {})
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


def test_cached_bash_is_reprobed_when_file_changes(monkeypatch, tmp_path):
    bash = tmp_path / "bash.exe"
    bash.write_bytes(b"old")
    monkeypatch.setattr(executor, "_PROBED", {})
    monkeypatch.setattr(executor.os, "name", "nt")
    monkeypatch.setenv("HERMES_HANDS_BASH", str(bash))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "none"))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    probes = []

    def probe(args, **kwargs):
        probes.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(executor.subprocess, "run", probe)
    assert executor.resolve_bash() == str(bash)
    bash.write_bytes(b"replacement with a different size")
    assert executor.resolve_bash() == str(bash)
    assert probes == [[str(bash), "-c", "exit 0"]] * 2


def test_failed_cached_bash_reprobe_uses_next_candidate(monkeypatch, tmp_path):
    preferred = tmp_path / "preferred.exe"
    fallback = tmp_path / "fallback.exe"
    preferred.write_bytes(b"old")
    fallback.write_bytes(b"bash")
    monkeypatch.setattr(executor, "_PROBED", {})
    monkeypatch.setattr(executor.os, "name", "nt")
    monkeypatch.setenv("HERMES_HANDS_BASH", str(preferred))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "none"))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: str(fallback))
    probes = []

    def probe(args, **kwargs):
        probes.append(args[0])
        return subprocess.CompletedProcess(args, int(args[0] == str(preferred) and probes.count(str(preferred)) > 1))

    monkeypatch.setattr(executor.subprocess, "run", probe)
    assert executor.resolve_bash() == str(preferred)
    preferred.write_bytes(b"replacement with a different size")
    assert executor.resolve_bash() == str(fallback)
    assert probes == [str(preferred), str(preferred), str(fallback)]


def test_real_bash_execution(monkeypatch, tmp_path):
    bash = executor.resolve_bash()
    if not bash:
        pytest.skip("bash is unavailable")
    monkeypatch.setattr(executor, "resolve_bash", lambda: bash)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    chunks = b"".join(executor.run("echo hi", timeout=5))
    assert chunks.strip() == b"hi"


@pytest.mark.skipif(os.name != "nt", reason="MSYS switch conversion is Windows-only")
def test_cmd_slash_switch_can_be_passed_without_msys_path_conversion(monkeypatch, tmp_path):
    bash = executor.resolve_bash()
    if not bash:
        pytest.skip("Git Bash is unavailable")
    monkeypatch.setattr(executor, "resolve_bash", lambda: bash)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    output = b"".join(executor.run("MSYS_NO_PATHCONV=1 cmd /c echo x", timeout=15))

    assert output.strip() == b"x"


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
            f'nohup python "{script_file.as_posix()}" >/dev/null 2>&1 & echo $!', timeout=15
        )
    )
    elapsed = time.monotonic() - started
    assert output.splitlines()[0].isdigit()
    deadline = time.monotonic() + 15
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    child_pid = int(pid_file.read_text(encoding="utf-8-sig"))
    try:
        assert elapsed < 17
        assert psutil.pid_exists(child_pid)
    finally:
        if psutil.pid_exists(child_pid):
            psutil.Process(child_pid).kill()


def test_chatty_background_writer_is_bounded_after_shell_exit(monkeypatch, tmp_path):
    bash = executor.resolve_bash()
    if not bash:
        pytest.skip("bash is unavailable")
    monkeypatch.setattr(executor, "resolve_bash", lambda: bash)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    pid_file = tmp_path / "writer.pid"
    script_file = tmp_path / "writer.py"
    script_file.write_text(
        f"import os, sys, time\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\nwhile True:\n print('tick', flush=True)\n time.sleep(0.01)\n",
        encoding="utf-8",
    )
    started = time.monotonic()
    chunks = b"".join(executor.run(f'python "{script_file.as_posix()}" & echo parent-exit', timeout=15))
    elapsed = time.monotonic() - started
    deadline = time.monotonic() + 15
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if pid_file.exists():
        child_pid = int(pid_file.read_text(encoding="utf-8-sig"))
        if psutil.pid_exists(child_pid):
            psutil.Process(child_pid).kill()
    assert b"parent-exit" in chunks
    assert b"tick" in chunks or b"[output truncated after the post-exit idle limit]" in chunks or b"[device timeout]" in chunks
    assert elapsed < 17


def test_large_script_runs_from_temp_file_and_is_removed(monkeypatch, tmp_path):
    bash = executor.resolve_bash()
    if not bash:
        pytest.skip("bash is unavailable")
    monkeypatch.setattr(executor, "resolve_bash", lambda: bash)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    script = "printf x\n" * 100_000
    assert b"".join(executor.run(script, timeout=30)) == b"x" * 100_000
    assert not list((tmp_path / ".hermes-hands/out").glob("*.sh"))


def test_timeout_kills_process(monkeypatch, tmp_path):
    monkeypatch.setattr(executor, "resolve_bash", lambda: shutil.which("bash"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    started = time.monotonic()
    chunks = b"".join(executor.run("sleep 10", timeout=0.1))
    assert b"timeout" in chunks
    assert time.monotonic() - started < 3


@pytest.mark.skipif(os.name == "nt", reason="POSIX deadline timer race")
def test_posix_timeout_always_reports_timeout_marker(monkeypatch, tmp_path):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is unavailable")
    monkeypatch.setattr(executor, "resolve_bash", lambda: bash)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for _ in range(20):
        exits = []
        chunks = b"".join(executor.run("sleep 1", timeout=0.01, on_exit=exits.append))
        assert b"[device timeout]" in chunks
        assert exits == [124]


@pytest.mark.skipif(os.name == "nt", reason="POSIX process group is covered by timeout test")
def test_grandchild_killed_on_posix(monkeypatch, tmp_path):
    monkeypatch.setattr(executor, "resolve_bash", lambda: shutil.which("bash"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    chunks = b"".join(executor.run("sleep 30 & echo $!; wait", timeout=0.2))
    child_pid = int(chunks.splitlines()[0])
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)  # windows-footgun: ok -- POSIX-only test for killpg behavior


@pytest.mark.skipif(os.name == "nt", reason="POSIX process group is covered by timeout test")
def test_recorded_process_reap_kills_process_group(monkeypatch):
    calls = []
    monkeypatch.setattr(executor.psutil, "Process", lambda pid: type("Process", (), {"create_time": lambda self: 1.0})())
    monkeypatch.setattr(executor.os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    executor.terminate_recorded(4321, 1.0)
    assert calls == [(4321, executor.signal.SIGKILL)]


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
    chunks = b"".join(executor.run(f'python "{script_file.as_posix()}"', timeout=30))
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


def test_stdin_is_staged_byte_exact_and_removed(monkeypatch, tmp_path):
    bash = executor.resolve_bash()
    if not bash:
        pytest.skip("bash is unavailable")
    monkeypatch.setattr(executor, "resolve_bash", lambda: bash)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert b"".join(executor.run("cat", stdin="hello \u4e2d\u6587\n", timeout=5)) == "hello \u4e2d\u6587\n".encode()
    assert not list((tmp_path / ".hermes-hands/out").glob("tmp*"))


def test_stdin_uses_surrogateescape_and_is_removed(monkeypatch, tmp_path):
    bash = executor.resolve_bash()
    if not bash:
        pytest.skip("bash is unavailable")
    monkeypatch.setattr(executor, "resolve_bash", lambda: bash)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    raw = b"before\xffafter"
    stdin = raw.decode("utf-8", errors="surrogateescape")
    assert b"".join(executor.run("cat", stdin=stdin, timeout=5)) == raw
    assert not list((tmp_path / ".hermes-hands/out").glob("*.stdin"))


def test_stdin_base64_is_staged_byte_exact_and_removed(monkeypatch, tmp_path):
    bash = executor.resolve_bash()
    if not bash:
        pytest.skip("bash is unavailable")
    monkeypatch.setattr(executor, "resolve_bash", lambda: bash)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    raw = b"before\xffafter"
    encoded = base64.b64encode(raw).decode("ascii")
    assert b"".join(executor.run("cat", stdin_b64=encoded, timeout=5)) == raw
    assert not list((tmp_path / ".hermes-hands/out").glob("*.stdin"))


def test_taskkill_timeout_falls_back_without_raising(monkeypatch):
    class Process:
        pid = 4321
        killed = False

        def kill(self):
            self.killed = True

        def children(self, recursive=False):
            return []

    proc = Process()
    calls = []
    monkeypatch.setattr(executor.os, "name", "nt")
    monkeypatch.setattr(executor.psutil, "Process", lambda pid: type("Live", (), {"create_time": lambda self: 1.0, "children": lambda self, recursive=False: []})())
    monkeypatch.setattr(executor, "_open_windows_job", lambda pid, start_time: None)
    monkeypatch.setattr(executor.shutil, "which", lambda name: "taskkill.exe")

    def slow_taskkill(args, **kwargs):
        calls.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(executor.subprocess, "run", slow_taskkill)
    executor._terminate(proc, 1.0)
    assert calls == [10]
    assert proc.killed


@pytest.mark.parametrize("failure", ["record", "start"])
def test_executor_start_failure_kills_process_and_cleans_staged_files(monkeypatch, tmp_path, failure):
    bash = executor.resolve_bash()
    if not bash:
        pytest.skip("bash is unavailable")
    monkeypatch.setattr(executor, "resolve_bash", lambda: bash)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    killed = []
    terminate_process = executor._terminate

    def terminate(proc, started):
        killed.append(proc.pid)
        terminate_process(proc, started)

    monkeypatch.setattr(executor, "_terminate", terminate)
    if failure == "record":
        monkeypatch.setattr(executor, "record_process", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
        on_start = lambda *args: None
        error = OSError
    else:
        on_start = lambda *args: (_ for _ in ()).throw(RuntimeError("start failed"))
        error = RuntimeError

    with pytest.raises(error):
        list(executor.run("sleep 30", job_id=f"failure-{failure}", stdin="private input", on_start=on_start))
    assert len(killed) == 1
    out = tmp_path / ".hermes-hands" / "out"
    assert not list(out.glob("*.sh"))
    assert not list(out.glob("*.stdin"))
    assert not list(out.glob("*.process.json"))


@pytest.mark.skipif(os.name != "nt", reason="MSYS child process behavior")
def test_msys_child_is_killed_on_timeout(monkeypatch, tmp_path):
    bash = executor.resolve_bash()
    if not bash:
        pytest.skip("Git Bash is unavailable")
    monkeypatch.setattr(executor, "resolve_bash", lambda: bash)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    token = f"hands-{time.time_ns()}"
    chunks = b"".join(executor.run(f"sleep 0.5; yes {token} > /dev/null; wait", timeout=2))
    assert b"[device timeout]" in chunks
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        survivor = [p for p in psutil.process_iter(["name", "cmdline"]) if p.info["name"] and p.info["name"].lower() == "yes.exe" and token in " ".join(p.info["cmdline"] or [])]
        if not survivor:
            break
        time.sleep(0.05)
    assert not survivor


def test_orphaned_stdin_files_are_reaped_at_start(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    out = tmp_path / ".hermes-hands" / "out"
    out.mkdir(parents=True)
    staged = out / "abandoned.stdin"
    staged.write_bytes(b"left over from an interrupted run")
    executor.reap_recorded_processes()
    assert not staged.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows Python UTF-8 environment")
def test_windows_python_utf8_environment_is_passed_to_child(monkeypatch, tmp_path):
    bash = executor.resolve_bash()
    if not bash:
        pytest.skip("Git Bash is unavailable")
    monkeypatch.setattr(executor, "resolve_bash", lambda: bash)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    output = b"".join(executor.run("python -c 'import os; print(os.environ[\"PYTHONUTF8\"] + \" \" + os.environ[\"PYTHONIOENCODING\"])'", timeout=15))
    assert output.strip() == b"1 utf-8"


def test_output_cap_reports_nonzero_exit(monkeypatch, tmp_path):
    bash = executor.resolve_bash()
    if not bash:
        pytest.skip("bash is unavailable")
    monkeypatch.setattr(executor, "resolve_bash", lambda: bash)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(executor, "MAX_OUTPUT", 5)
    exits = []
    capped = []
    list(executor.run("printf 123456789", timeout=5, on_exit=exits.append, on_capped=lambda: capped.append(True)))
    assert exits == [1]
    assert capped == [True]


def test_reader_queue_is_bounded():
    assert 0 < executor.OUTPUT_QUEUE_SIZE < 1024


def test_recorded_process_is_reaped_and_record_removed(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    killed = []
    monkeypatch.setattr(executor, "terminate_recorded", lambda pid, created: killed.append((pid, created)))
    executor.record_process("old-job", 1234, 5678.5)
    record = executor.record_path("old-job")
    assert record.exists()
    executor.reap_recorded_processes()
    assert killed == [(1234, 5678.5)]
    assert not record.exists()


@pytest.mark.parametrize("contents", ["", '{"pid": 12'])
def test_corrupt_process_record_does_not_stop_reaping(monkeypatch, tmp_path, contents):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    out = tmp_path / ".hermes-hands" / "out"
    out.mkdir(parents=True)
    record = out / "torn.process.json"
    record.write_text(contents, encoding="utf-8")
    executor.reap_recorded_processes()
    assert not record.exists()


def test_reaped_process_start_time_mismatch_does_not_kill(monkeypatch):
    calls = []
    monkeypatch.setattr(executor.psutil, "Process", lambda pid: type("Process", (), {"create_time": lambda self: 1.0, "kill": lambda self: calls.append(pid)})())
    monkeypatch.setattr(executor.os, "name", "nt")
    executor.terminate_recorded(1234, 2.0)
    assert calls == []


def test_recorded_posix_reap_kills_the_process_group(monkeypatch):
    killed = []
    process = type("Process", (), {"create_time": lambda self: 2.0})()
    monkeypatch.setattr(executor.os, "name", "posix")
    monkeypatch.setattr(executor.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(executor.psutil, "Process", lambda pid: process)
    monkeypatch.setattr(executor.os, "killpg", lambda pid, sig: killed.append((pid, sig)), raising=False)
    executor.terminate_recorded(1234, 2.0)
    assert killed == [(1234, executor.signal.SIGKILL)]

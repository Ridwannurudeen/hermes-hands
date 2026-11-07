from __future__ import annotations

import os
import json
import queue
import re
import shutil
import signal
import subprocess
import threading
import tempfile
import time
from pathlib import Path
from typing import Iterator

import psutil

MAX_OUTPUT = 64 * 1024 * 1024
CHUNK_SIZE = 16 * 1024
PIPE_DRAIN_GRACE = 0.2
OUTPUT_QUEUE_SIZE = 8
_PROBED: dict[str, tuple[int, int, int]] = {}  # file identity for candidates whose probe passed


def _is_stub(path: str) -> bool:
    lowered = os.path.normcase(os.path.normpath(path)).lower().replace("/", "\\")
    return "\\system32\\" in lowered or "\\windowsapps\\" in lowered


def resolve_bash() -> str | None:
    candidates: list[str] = []
    if os.name == "nt":
        env = os.environ
        if env.get("HERMES_HANDS_BASH"):
            candidates.append(env["HERMES_HANDS_BASH"])
        pf = env.get("ProgramFiles", r"C:\Program Files")
        roots = [os.path.join(pf, "Git")]
        if env.get("ProgramFiles(x86)"):
            roots.append(os.path.join(env["ProgramFiles(x86)"], "Git"))
        if env.get("LOCALAPPDATA"):
            roots.append(os.path.join(env["LOCALAPPDATA"], "Programs", "Git"))
        for root in roots:
            candidates.extend((os.path.join(root, "bin", "bash.exe"), os.path.join(root, "usr", "bin", "bash.exe")))
    which = shutil.which("bash")
    if which and (os.name != "nt" or not _is_stub(which)):
        candidates.append(which)
    for candidate in dict.fromkeys(candidates):
        if os.name == "nt" and _is_stub(candidate):
            continue
        try:
            stat = os.stat(candidate)
        except OSError:
            continue
        identity = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
        if candidate in _PROBED:
            if _PROBED[candidate] == identity:
                return candidate
            del _PROBED[candidate]
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            probe = subprocess.run([candidate, "-c", "exit 0"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, creationflags=flags, check=False)
            if probe.returncode == 0:
                _PROBED[candidate] = identity
                return candidate
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


def _terminate(proc: subprocess.Popen, start_time: float) -> None:
    if os.name == "nt":
        try:
            current = psutil.Process(proc.pid).create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return
        if abs(current - start_time) > 0.01:
            return
        taskkill = shutil.which("taskkill")
        if taskkill:
            subprocess.run([taskkill, "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        else:
            proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # windows-footgun: ok -- POSIX-only branch
        except ProcessLookupError:
            pass


def terminate_recorded(pid: int, start_time: float) -> None:
    try:
        process = psutil.Process(pid)
        current = process.create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return
    if abs(current - start_time) > 0.01:
        return
    if os.name == "nt":
        taskkill = shutil.which("taskkill")
        if taskkill:
            subprocess.run([taskkill, "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        else:
            process.kill()
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def record_path(job_id: str) -> Path:
    return Path.home() / ".hermes-hands" / "out" / f"{job_id}.process.json"


def record_process(job_id: str, pid: int, start_time: float) -> None:
    path = record_path(job_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "create_time": start_time}), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


def reap_recorded_processes() -> None:
    out_dir = Path.home() / ".hermes-hands" / "out"
    if not out_dir.exists():
        return
    for path in out_dir.glob("*.process.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        terminate_recorded(int(record["pid"]), float(record["create_time"]))
        path.unlink(missing_ok=True)


def run(script: str, *, login: bool = False, timeout: int = 120, job_id: str = "job", stdin: str | None = None, on_start=None, on_exit=None) -> Iterator[bytes]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
        raise ValueError("invalid job id")
    bash = resolve_bash()
    if not bash:
        raise RuntimeError("Git Bash was not found or failed its probe")
    script_dir = Path.home() / ".hermes-hands" / "out"
    script_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", suffix=".sh", dir=script_dir, delete=False) as script_file:
        script_file.write(script)
        script_path = Path(script_file.name)
    args = [bash, *( ["-l"] if login else []), str(script_path)]
    kwargs = {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {"start_new_session": True}
    stdin_path = None
    if stdin is not None:
        with tempfile.NamedTemporaryFile("wb", dir=script_dir, delete=False) as stdin_file:
            stdin_file.write(stdin.encode("utf-8"))
            stdin_path = Path(stdin_file.name)
        stdin_handle = stdin_path.open("rb")
    else:
        stdin_handle = subprocess.DEVNULL
    env = None
    if os.name == "nt":
        env = dict(os.environ)
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=stdin_handle, env=env, **kwargs)
    finally:
        if stdin_path is not None:
            stdin_handle.close()
    created = psutil.Process(proc.pid).create_time()
    if on_start:
        record_process(job_id, proc.pid, created)
        on_start(proc, created)
    chunks: queue.Queue[bytes | None] = queue.Queue(maxsize=OUTPUT_QUEUE_SIZE)
    deadline = time.monotonic() + timeout
    total = 0
    capped = threading.Event()
    exited = threading.Event()

    def kill_at_deadline() -> None:
        if proc.poll() is None:
            _terminate(proc, created)

    timer = threading.Timer(max(0, deadline - time.monotonic()), kill_at_deadline)
    timer.daemon = True
    timer.start()

    def read_output() -> None:
        nonlocal total
        while True:
            chunk = proc.stdout.read1(CHUNK_SIZE)
            if not chunk:
                while True:
                    try:
                        chunks.put(None, timeout=0.05)
                        return
                    except queue.Full:
                        if exited.is_set():
                            return
            available = MAX_OUTPUT - total
            if len(chunk) > available:
                chunk = chunk[:available]
                capped.set()
            total += len(chunk)
            while True:
                try:
                    chunks.put(chunk, timeout=0.05)
                    break
                except queue.Full:
                    if exited.is_set():
                        return
            if capped.is_set():
                _terminate(proc, created)
                return

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    output_path = Path.home() / ".hermes-hands" / "out" / f"{job_id}.log"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output_path.open("wb") as log:
            while True:
                now = time.monotonic()
                if now >= deadline and proc.poll() is None:
                    _terminate(proc, created)
                    if on_exit:
                        on_exit(124)
                    yield b"[device timeout]\n"
                    return
                try:
                    chunk = chunks.get(timeout=PIPE_DRAIN_GRACE if proc.poll() is not None else 0.1)
                except queue.Empty:
                    if proc.poll() is not None:
                        break
                    continue
                if chunk is None:
                    break
                log.write(chunk)
                yield chunk
            if capped.is_set():
                if on_exit:
                    on_exit(125)
                yield b"[output truncated at 64 MiB]\n"
                return
        proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        if on_exit:
            on_exit(proc.returncode)
    finally:
        exited.set()
        timer.cancel()
        if proc.poll() is None:
            _terminate(proc, created)
            proc.wait()
        script_path.unlink(missing_ok=True)
        if stdin_path is not None:
            stdin_path.unlink(missing_ok=True)
        record_path(job_id).unlink(missing_ok=True)

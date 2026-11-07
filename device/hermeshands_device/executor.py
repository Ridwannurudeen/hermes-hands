from __future__ import annotations

import os
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
_PROBED: set[str] = set()  # candidates whose probe passed: a probe costs a bash start, every job


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
        if not os.path.isfile(candidate):
            continue
        if candidate in _PROBED:
            return candidate
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            probe = subprocess.run([candidate, "-c", "exit 0"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, creationflags=flags, check=False)
            if probe.returncode == 0:
                _PROBED.add(candidate)
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


def run(script: str, *, login: bool = False, timeout: int = 120, job_id: str = "job", on_start=None, on_exit=None) -> Iterator[bytes]:
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
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kwargs)
    created = psutil.Process(proc.pid).create_time()
    if on_start:
        on_start(proc, created)
    chunks: queue.Queue[bytes | None] = queue.Queue()

    def read_output() -> None:
        while True:
            chunk = proc.stdout.read1(CHUNK_SIZE)
            if not chunk:
                chunks.put(None)
                return
            chunks.put(chunk)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    output_path = Path.home() / ".hermes-hands" / "out" / f"{job_id}.log"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    total = 0
    try:
        with output_path.open("wb") as log:
            exited_at = None
            while True:
                now = time.monotonic()
                if proc.poll() is not None and exited_at is None:
                    exited_at = now
                if exited_at is not None and now - exited_at >= PIPE_DRAIN_GRACE:
                    break
                if exited_at is None and now >= deadline:
                    _terminate(proc, created)
                    if on_exit:
                        on_exit(124)
                    yield b"[device timeout]\n"
                    return
                try:
                    wait_until = deadline if exited_at is None else exited_at + PIPE_DRAIN_GRACE
                    chunk = chunks.get(timeout=min(0.1, max(0.01, wait_until - now)))
                except queue.Empty:
                    continue
                if chunk is None:
                    break
                available = MAX_OUTPUT - total
                if available <= 0:
                    _terminate(proc, created)
                    yield b"[output truncated at 64 MiB]\n"
                    return
                chunk = chunk[:available]
                total += len(chunk)
                log.write(chunk)
                yield chunk
                if total >= MAX_OUTPUT:
                    _terminate(proc, created)
                    yield b"[output truncated at 64 MiB]\n"
                    return
        proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        if on_exit:
            on_exit(proc.returncode)
    finally:
        if proc.poll() is None:
            _terminate(proc, created)
            proc.wait()
        script_path.unlink(missing_ok=True)

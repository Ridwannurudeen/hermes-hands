"""``RemoteProcessHandle``: the ``ProcessHandle`` (``tools/environments/base_output.py:269-281``) for a
script running on a paired device, shaped like core's ``_ThreadedProcessHandle`` (``:284-338``).

A worker thread (started in a copy of the caller's context, so profile-scoped reads stay scoped)
queues the script at the relay and long-polls its output into an OS pipe whose read end is
``stdout``: a real descriptor, so core's drain thread uses its PeekNamedPipe/select path
(``base_output.py:342-477``). The write end is closed BEFORE ``poll()``/``returncode`` turn non-None;
the drain thread stops about 300 ms after exit, and output still in flight then would be lost
(``base.py:549-594``). There is deliberately no ``pid``: core's backstop kills ``pid`` as a host
process tree (``base.py:785-798``). Timeouts are core's: the device gets a later deadline, so
``poll()`` stays None past core's and core returns 124 with ``hermes_timed_out`` itself.
"""

from __future__ import annotations

import contextvars
import logging
import os
import threading

from . import client
from .relay import UNREACHABLE

DEVICE_TIMEOUT_GRACE = 15  # seconds the device waits past core's deadline before its own 124

logger = logging.getLogger(__name__)


class RemoteProcessHandle:
    def __init__(self, device: str, script: str, *, login: bool, timeout: int):
        read_fd, write_fd = os.pipe()
        self._stdout = os.fdopen(read_fd, "r", encoding="utf-8", errors="replace")
        self._write_fd = write_fd
        self._returncode: int | None = None
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._job_id: str | None = None
        self._killed = False
        self._reader_gone = False
        self._wrote = False
        worker = contextvars.copy_context()
        threading.Thread(
            target=worker.run,
            args=(self._run, device, script, login, timeout),
            name=f"hermes-hands-{device}",
            daemon=True,
        ).start()

    @property
    def stdout(self):
        return self._stdout

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def poll(self) -> int | None:
        return self._returncode if self._done.is_set() else None

    def wait(self, timeout: float | None = None) -> int:
        """The exit code; ``UNREACHABLE`` if it is still running after ``timeout`` (the Protocol's
        ``-> int``, base_output.py:275, where Popen would raise TimeoutExpired)."""
        if not self._done.wait(timeout=timeout):
            return UNREACHABLE
        return self._returncode

    def kill(self) -> None:
        """Cancel the job at the relay (the device stops it at its next poll) and drop further output."""
        with self._lock:
            if self._killed:
                return
            self._killed = True
            job_id = self._job_id
        if job_id is not None and not self._done.is_set():
            self._cancel(job_id)

    @staticmethod
    def _cancel(job_id: str) -> None:
        try:
            client.cancel_job(job_id)
        except client.HandsError:
            logger.warning(
                "hermes-hands: could not cancel job %s at the relay", job_id, exc_info=True
            )

    def _write(self, text: str) -> None:
        if not text or self._killed or self._reader_gone:
            return
        data = text.encode("utf-8")
        try:
            while data:
                data = data[os.write(self._write_fd, data) :]
        except OSError:
            self._reader_gone = True  # core closed stdout: nobody reads any more
        self._wrote = True

    def _write_notice(self, text: str) -> None:
        """A hands: line after the command's own output, on a line of its own."""
        self._write(("\n" if self._wrote else "") + text)

    def _run(self, device: str, script: str, login: bool, timeout: int) -> None:
        returncode = UNREACHABLE
        try:
            job_id = client.start_job(
                device, script=script, login=login, timeout=timeout + DEVICE_TIMEOUT_GRACE
            )
            with self._lock:
                self._job_id = job_id
                killed = self._killed
            if killed:
                self._cancel(job_id)
            after = 0
            while True:
                out = client.job_output(job_id, after)
                for chunk in out["chunks"]:
                    self._write(chunk)
                after = out["next"]
                if out["done"]:
                    if out.get("error"):
                        self._write_notice(str(out["error"]))
                    returncode = (
                        UNREACHABLE if out.get("returncode") is None else int(out["returncode"])
                    )
                    return
        except client.HandsError as exc:
            self._write_notice(str(exc))
        finally:
            os.close(self._write_fd)  # EOF reaches the drain thread before poll() reports the exit
            self._returncode = returncode
            self._done.set()

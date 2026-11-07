"""The ``hands`` terminal backend: one environment object that runs each call on the target the
conversation chose (``targets.resolve_target``), through one lazily created sub-environment per target.

``server`` is a plain ``LocalEnvironment`` on the gateway host and never touches the relay. A device
is a :class:`DeviceEnvironment`, whose commands travel through the relay as signed jobs; an offline,
revoked or refused device, or a relay that is down, fails the call with 255 and nothing runs anywhere
else.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import sqlite3
import threading

from gateway.session_context import get_session_env
from tools.environments.base import BaseEnvironment
from tools.environments.local import LocalEnvironment

from . import messages, store, targets
from .handle import RemoteProcessHandle
from .relay import UNREACHABLE

logger = logging.getLogger(__name__)

_DRIVE_PATH = re.compile(r"^([A-Za-z]):[\\/]*(.*)$")


def device_path(path: str) -> str:
    """A native Windows path (``C:\\Users\\x``, ``C:/Users/x``) in its Git Bash form ``/c/Users/x``;
    any other path unchanged. Core does this only for the local backend (``local.py:172-189``)."""
    match = _DRIVE_PATH.match(path)
    if match is None:
        return path
    tail = match.group(2).replace("\\", "/")
    return f"/{match.group(1).lower()}/{tail}"


class DeviceEnvironment(BaseEnvironment):
    """A paired device: core's spawn-per-call flow, with every bash run sent to the device as a job."""

    # write_file/patch content is embedded in the script as a heredoc (base.py:268-272, 702-705).
    _stdin_mode = "heredoc"

    def __init__(self, name: str, *, cwd: str, timeout: int, home: str | None):
        super().__init__(cwd=cwd, timeout=timeout)
        self.name = name
        self.home = (
            home  # the home it reported at pairing, Git Bash form; None for an unknown device
        )
        self._init_lock = threading.Lock()
        self._session_started = False

    def _run_bash(
        self,
        cmd_string: str,
        *,
        login: bool = False,
        timeout: int = 120,
        stdin_data: str | None = None,
    ) -> RemoteProcessHandle:
        assert not stdin_data, "heredoc stdin mode embeds stdin in the script"
        return RemoteProcessHandle(self.name, cmd_string, login=login, timeout=timeout)

    def execute(self, command: str, cwd: str = "", **kwargs) -> dict:
        """Capture the device's login shell on first use, not at construction (no network there). A
        bootstrap that got no usable shell (device offline, relay down) is retried on the next call."""
        with self._init_lock:
            if not self._session_started:
                self.init_session()
                self._session_started = self._snapshot_ready or self._prefer_nonlogin
        return super().execute(command, cwd, **kwargs)

    @staticmethod
    def _quote_cwd_for_cd(cwd: str) -> str:
        return BaseEnvironment._quote_cwd_for_cd(device_path(cwd))

    def _quote_shell_path(self, path: str) -> str:
        return shlex.quote(device_path(path))

    def cleanup(self):
        """Nothing to release on the gateway; the device keeps its snapshot file in its own temp dir."""


class HandsEnvironment(BaseEnvironment):
    """``terminal.backend: hands``. ``BaseEnvironment.__init__`` is deliberately not called: it assigns
    ``cwd`` and ``env``, which here are proxies to the current target's sub-environment, and the
    snapshot state it sets up belongs to each sub-environment."""

    # Keeps core from tearing the backend down after every turn (terminal_tool_lifecycle.py:258-271,
    # agent/chat_completion_helpers.py:2334-2353); the idle reaper and AIAgent.close still clean up.
    _persistent = True

    def __init__(self, ctx, *, cwd: str, timeout: int):
        self._ctx = ctx
        self._requested_cwd = cwd
        self._timeout = timeout
        self._lock = threading.Lock()
        self._subs: dict[str, BaseEnvironment] = {}
        self._store_failure_logged = False
        # A new object notes the target of its first terminal command. Where the session last ran
        # comes from the store: this object may be new since then (idle reaper, gateway restart).
        self._noted_target: str | None = None
        session_key = get_session_env("HERMES_SESSION_KEY", "")
        try:
            self._last_target = store.get_last_run(session_key) if session_key else None
        except sqlite3.Error:
            self._store_failed()
            self._last_target = None
        # Create the first target now so ``cwd`` is already absolute when core stamps the sandbox
        # home right after create_environment (terminal_tool_backends.py:213-233, 256).
        self._current()

    # --- target resolution -----------------------------------------------------------------
    def _store_failed(self) -> None:
        if not self._store_failure_logged:
            self._store_failure_logged = True
            logger.warning("hermes-hands: the device store is unreadable", exc_info=True)

    def _sub(self, target: str) -> tuple[str, BaseEnvironment]:
        with self._lock:
            sub = self._subs.get(target)
            if sub is None:
                sub = self._subs[target] = self._create(target)
        return target, sub

    def _resolve(self) -> tuple[str, BaseEnvironment]:
        """This call's target and its sub-environment; raises ``sqlite3.Error`` if the store is unreadable."""
        return self._sub(
            targets.resolve_target(
                session_key=get_session_env("HERMES_SESSION_KEY", ""),
                in_cron=self._ctx.current_cron_execution() is not None,
                default_target=str(self._ctx.get_config("default_target", targets.SERVER)),
            )
        )

    def _current(self) -> tuple[str, BaseEnvironment]:
        """``_resolve`` for core's attribute reads, which must never raise (file_operations.py:238-239,
        terminal_tool.py:358-362, terminal_tool_backends.py:226): with the store unreadable the server
        sub-environment answers them, while :meth:`execute` refuses to run anything."""
        try:
            return self._resolve()
        except sqlite3.Error:
            self._store_failed()
            return self._sub(targets.SERVER)

    def _create(self, target: str) -> BaseEnvironment:
        requested = None if self._requested_cwd == "~" else self._requested_cwd
        if target == targets.SERVER:
            persistent = os.getenv("TERMINAL_LOCAL_PERSISTENT", "true").strip().lower() in {
                "true",
                "1",
                "yes",
            }
            cwd = requested or os.getenv("MESSAGING_CWD") or os.getcwd()
            return LocalEnvironment(cwd=cwd, timeout=self._timeout, persistent=persistent)
        device = store.get_device(target)
        home = device_path(device["home"]) if device is not None else None
        return DeviceEnvironment(
            target, cwd=requested or home or "~", timeout=self._timeout, home=home
        )

    def _note(self, target: str) -> str:
        if target == targets.SERVER:
            return messages.TARGET_NOTE_SERVER
        device = store.get_device(target)
        if device is None:
            return messages.target_note(target, None, None)
        return messages.target_note(target, device["os"], device_path(device["home"]))

    # --- delegated surface -----------------------------------------------------------------
    def execute(self, command: str, cwd: str = "", **kwargs) -> dict:
        note = None
        try:
            # Every store read and write happens before the command runs: an error after it would
            # make the terminal tool retry, i.e. run the command again (terminal_tool.py:1363-1390).
            target, sub = self._resolve()
            # Only the foreground terminal tool sets bounded_capture (base.py:679-681); the file tools
            # parse their raw output, so the note never goes there.
            if kwargs.get("bounded_capture") and target != self._noted_target:
                note = self._note(target)
            session_key = get_session_env("HERMES_SESSION_KEY", "")
            if session_key and target != self._last_target:
                store.set_last_run(session_key, target)
        except sqlite3.Error:
            self._store_failed()
            return {"output": messages.STORE_DOWN, "returncode": UNREACHABLE}
        if cwd == "~" or (self._last_target is not None and target != self._last_target):
            # ``~`` is the generic default and a cwd recorded before a switch names the other
            # target's directory: keep this target's own cwd instead.
            cwd = ""
        self._last_target = target
        if note is not None:
            self._noted_target = target
        result = sub.execute(command, cwd, **kwargs)
        if note is not None:
            result["output"] = f"{note}\n{result.get('output', '')}"
        return result

    def fetch_file(self, remote_path, local_dest, *, max_bytes: int) -> None:
        self._resolve()[1].fetch_file(remote_path, local_dest, max_bytes=max_bytes)

    def fetch_realpath(self, remote_path: str) -> str | None:
        return self._resolve()[1].fetch_realpath(remote_path)

    def get_temp_dir(self) -> str:
        return self._current()[1].get_temp_dir()

    @property
    def cwd(self) -> str:
        return self._current()[1].cwd

    @cwd.setter
    def cwd(self, value: str) -> None:
        self._current()[1].cwd = value

    @property
    def env(self) -> dict:
        return self._current()[1].env

    @property
    def is_local(self) -> bool:
        return self._current()[1].is_local

    @property
    def uses_host_filesystem(self) -> bool:
        return self._current()[1].uses_host_filesystem

    @property
    def _hermes_sandbox_home(self) -> str | None:
        """The current target's home, which core reads to anchor relative file-tool paths and to keep
        recorded cwds (terminal_tool_config.py:188-201, file_tools_paths.py:281-301,
        terminal_tool.py:1041-1047): a device's paired home, the server's cwd."""
        target, sub = self._current()
        return sub.cwd if target == targets.SERVER else sub.home

    @_hermes_sandbox_home.setter
    def _hermes_sandbox_home(self, value: str) -> None:
        """Ignored: core stamps the first target's home once (terminal_tool_backends.py:213-233), and
        the home has to follow /hands use."""

    def cleanup(self):
        with self._lock:
            subs = list(self._subs.values())
            self._subs.clear()
        for sub in subs:
            sub.cleanup()

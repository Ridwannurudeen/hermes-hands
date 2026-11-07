"""The ``hands`` terminal backend: one environment object that runs each call on the target the
conversation chose (``targets.resolve_target``), through one lazily created sub-environment per target.

``server`` is a plain ``LocalEnvironment`` on the gateway host and never touches the relay. A device
is a :class:`DeviceEnvironment`, whose commands travel through the relay as signed jobs; an offline,
revoked or refused device, or a relay that is down, fails the call with 255 and nothing runs anywhere
else.
"""

from __future__ import annotations

import base64
import contextvars
import json
import logging
import os
import posixpath
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
# Core's background launcher and poller name their files after the registry session
# (process_registry.py:1271-1300, 1418-1466) and kill by pid (:2245-2246).
_BACKGROUND_FILE = re.compile(r"hermes_bg_([\w-]+)\.(?:log|pid|exit)\b")
_BACKGROUND_KILL = re.compile(r"kill (\d+) 2>/dev/null")
# The end of the launcher itself, which prints the pid it wrote (process_registry.py:1282-1288).
_BACKGROUND_LAUNCH = re.compile(r"\) & echo \$! > (.+) && cat \1\Z")
# One single-quoted argument as core's file tools quote it (file_operations.py:535-557).
_QUOTED = re.compile(r"'((?:[^']|'\"'\"')*)'")
# A Windows path core joined onto the base, which it does to anything not POSIX-absolute for a
# container backend (file_tools_paths.py:157-163): ``<home>/C:\Users\x`` or ``<home>/C:/Users/x``.
_JOINED_DRIVE = re.compile(r"/([A-Za-z]:(?:[\\/].*)?)\Z", re.S)
# Core's python snippets (file_operations.py:722-737) and the path they act on (:748, :1317).
_PYTHON_SNIPPET = re.compile(
    r"(python3?) -c \"import base64; exec\(base64\.b64decode\('([A-Za-z0-9+/=]*)'\)\.decode\(\)\)\""
)
_SNIPPET_PATH = re.compile(r"^p = (?:pathlib\.Path\()?(\"(?:[^\"\\]|\\.)*\")", re.M)
# Set by DeviceEnvironment.execute for one call and read in _run_bash, which core runs on its wait
# worker in a copy of the caller's context (agent/deadline.py:340-361).
_INLINE_LIMIT: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "hermes_hands_inline_limit", default=None
)


def device_path(path: str) -> str:
    """A native Windows path (``C:\\Users\\x``, ``C:/Users/x``) in its Git Bash form ``/c/Users/x``;
    any other path unchanged. Core does this only for the local backend (``local.py:172-189``)."""
    match = _DRIVE_PATH.match(path)
    if match is None:
        return path
    tail = match.group(2).replace("\\", "/")
    return f"/{match.group(1).lower()}/{tail}"


def _gateway_home() -> str | None:
    """The home core's file tools put in place of a model's ``~`` (file_tools_paths.py:32-49, 147-154),
    when it is a POSIX path; a Windows gateway's home could never appear in a device command."""
    from hermes_constants import get_subprocess_home

    home = (get_subprocess_home() or os.path.expanduser("~")).rstrip("/")
    return home if home and posixpath.isabs(home) else None


def _hands_failure(result: dict) -> bool:
    """A result the gateway half made up (handle.py, relay.py): a non-zero exit with a ``hands:`` line."""
    output = result.get("output") or ""
    return result.get("returncode") != 0 and any(
        line.startswith("hands:") for line in output.splitlines()
    )


class DeviceEnvironment(BaseEnvironment):
    """A paired device: core's spawn-per-call flow, with every bash run sent to the device as a job."""

    # stdin (write_file and patch content) travels beside the script, so the device's deny-list
    # sees only the command (base.py:268-272, 701-705, 727).
    _stdin_mode = "payload"

    def __init__(
        self,
        name: str,
        *,
        cwd: str,
        timeout: int,
        home: str | None,
        windows: bool,
        inline_limit: int,
    ):
        super().__init__(cwd=cwd, timeout=timeout)
        self.name = name
        self.home = (
            home  # the home it reported at pairing, Git Bash form; None for an unknown device
        )
        self.windows = windows
        self.inline_limit = inline_limit
        gateway_home = _gateway_home()
        self._gateway_home = (
            gateway_home if gateway_home and home and gateway_home != home else None
        )
        self._init_lock = threading.Lock()
        self._session_started = False

    def _file_path(self, path: str) -> str:
        """A file-tool path as the device names it: a gateway-home prefix, which is where core's file
        tools expanded a model's ``~``, becomes the device home (the target note gives the model that
        home), and on Windows a drive path core joined onto the base becomes its ``/c/...`` form."""
        gateway = self._gateway_home
        if gateway is not None and (path == gateway or path.startswith(gateway + "/")):
            path = self.home + path[len(gateway) :]
        if self.windows:
            drive = _JOINED_DRIVE.search(path)
            if drive is not None:
                path = device_path(drive.group(1))
        return path

    def _quoted_path(self, match: re.Match) -> str:
        value = match.group(1).replace("'\"'\"'", "'")
        mapped = self._file_path(value)
        return match.group(0) if mapped == value else shlex.quote(mapped)

    def _python_snippet(self, match: re.Match) -> str:
        """Core's python snippet with its path as the device names it. Native Windows Python reads
        ``/c/Users/x`` as ``C:\\c\\Users\\x`` (and a delete then succeeds without deleting anything), so
        there the device's own Git Bash converts the path (``cygpath -m``) and Python takes it from argv."""
        python, encoded = match.groups()
        program = base64.b64decode(encoded).decode("utf-8")
        literal = _SNIPPET_PATH.search(program)
        if literal is None:
            return match.group(0)
        path = json.loads(literal.group(1))
        mapped = self._file_path(path)
        argument = ""
        if self.windows and mapped.startswith("/"):
            replacement = '__import__("sys").argv[1]'
            argument = f' "$(cygpath -m {shlex.quote(mapped)})"'
        elif mapped != path:
            replacement = json.dumps(mapped)
        else:
            return match.group(0)
        program = program[: literal.start(1)] + replacement + program[literal.end(1) :]
        encoded = base64.b64encode(program.encode("utf-8")).decode("ascii")
        return (
            f"{python} -c \"import base64; exec(base64.b64decode('{encoded}').decode())\"{argument}"
        )

    def _file_command(self, command: str) -> str:
        """A file-tool command with its paths as the device names them (:meth:`_file_path`), each
        substituted path quoted for its place. Core builds every path argument as one single-quoted
        word (file_operations.py:535-557), and its python snippets carry theirs inside base64."""
        snippet = _PYTHON_SNIPPET.fullmatch(command)
        if snippet is not None:
            return self._python_snippet(snippet)
        return _QUOTED.sub(self._quoted_path, command)

    def _prepare_command(self, command: str) -> tuple[str, None]:
        """The command unchanged. Core's version (base.py:807-819) adds SUDO_PASSWORD or the cached
        sudo password to stdin (terminal_tool_sudo.py:471-500) and rewrites ``open`` when the gateway
        runs macOS (terminal_tool_macos_open.py:154): both describe the gateway host, not the device,
        and the device's deny-list refuses sudo anyway."""
        return command, None

    def _run_bash(
        self,
        cmd_string: str,
        *,
        login: bool = False,
        timeout: int = 120,
        stdin_data: str | None = None,
    ) -> RemoteProcessHandle:
        return RemoteProcessHandle(
            self.name,
            cmd_string,
            login=login,
            timeout=timeout,
            inline_limit=_INLINE_LIMIT.get(),
            stdin=stdin_data,
        )

    def execute(self, command: str, cwd: str = "", **kwargs) -> dict:
        """Capture the device's login shell on first use, not at construction (no network there). A
        bootstrap that got no usable shell (device offline, relay down) is retried on the next call.
        Only terminal-tool calls (``bounded_capture``, base.py:679-681) get the inline output limit:
        file reads through ``cat`` need every byte. Only file-tool calls get their paths mapped
        (:meth:`_file_command`): a terminal command or a background job runs exactly as approved."""
        with self._init_lock:
            if not self._session_started:
                self.init_session()
                self._session_started = self._snapshot_ready or self._prefer_nonlogin
        terminal = kwargs.get("bounded_capture")
        if not terminal and _BACKGROUND_FILE.search(command) is None:
            command = self._file_command(command)
        token = _INLINE_LIMIT.set(self.inline_limit if terminal else None)
        try:
            return super().execute(command, cwd, **kwargs)
        finally:
            _INLINE_LIMIT.reset(token)

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
        # Sub-environments are dropped only by cleanup(), so a background job's still exists after
        # any /hands use; core cleans up only an idle environment, and a running background job
        # keeps it active (terminal_tool_lifecycle.py:143-152).
        self._subs: dict[str, BaseEnvironment] = {}
        self._background: dict[str, str] = {}  # registry session id -> target it runs on
        # Its pid -> the targets a job with that pid was launched on: pids repeat across machines.
        self._background_pids: dict[int, set[str]] = {}
        self._store_failure_logged = False
        # A new object notes the target of its first terminal command. Where the session's last
        # terminal command ran comes from the store: this object may be new since then (idle reaper,
        # gateway restart).
        self._noted_target: str | None = None
        session_key = get_session_env("HERMES_SESSION_KEY", "")
        try:
            self._last_target = store.get_last_run(session_key) if session_key else None
        except sqlite3.Error:
            self._store_failed()
            self._last_target = None
        self._files_target: str | None = None
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
        target, sub = self._sub(
            targets.resolve_target(
                session_key=get_session_env("HERMES_SESSION_KEY", ""),
                in_cron=self._ctx.current_cron_execution() is not None,
                default_target=str(self._ctx.get_config("default_target", targets.SERVER)),
            )
        )
        if target != self._files_target:
            if self._files_target is not None:
                self._forget_file_ops()
            self._files_target = target
        return target, sub

    def _forget_file_ops(self) -> None:
        """Core keeps one ShellFileOperations per environment (file_tools.py:265-266, 312-375), and it
        caches which executables exist and where rg is (file_operations.py:225-227,
        file_operations_search.py:518-590) for whichever machine answered first. The target just
        changed, so those answers are another machine's; a file tool reads an attribute of this object
        before its first lookup (file_operations.py:238, file_operations_search.py:785-788)."""
        from tools import file_tools

        with file_tools._file_ops_lock:
            stale = [ops for ops in file_tools._file_ops_cache.values() if ops.env is self]
        for ops in stale:
            ops._command_cache.clear()
            ops._rg_resolution_cache.clear()
            ops._rg_modified_capability.clear()

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
        """``terminal.cwd`` (or a Desktop or ACP workspace) is a directory on the gateway host, so only
        the server starts there; a device starts in the home it reported at pairing."""
        if target == targets.SERVER:
            requested = None if self._requested_cwd == "~" else self._requested_cwd
            persistent = os.getenv("TERMINAL_LOCAL_PERSISTENT", "true").strip().lower() in {
                "true",
                "1",
                "yes",
            }
            cwd = requested or os.getenv("MESSAGING_CWD") or os.getcwd()
            # The relay seed is blank in every command: core strips the provider's strip_env_keys
            # from background and helper processes (local.py:333-337) but not from the terminal's
            # own spawn env, which takes this env last (local.py:776-787, 1136).
            return LocalEnvironment(
                cwd=cwd,
                timeout=self._timeout,
                env={"HANDS_RELAY_SEED": ""},
                persistent=persistent,
            )
        device = store.get_device(target)
        home = device_path(device["home"]) if device is not None else None
        return DeviceEnvironment(
            target,
            cwd=home or "~",
            timeout=self._timeout,
            home=home,
            windows=device is not None and device["os"].lower().startswith("windows"),
            inline_limit=int(self._ctx.get_config("inline_output_limit_bytes", 1048576)),
        )

    def _background_owner(self, command: str) -> str | None:
        """The target a background job's poll or kill belongs to, whatever the session chose since.
        Core's kill names only the pid (process_registry.py:2245-2246): a pid launched on two targets
        is refused, which core reports as a failed kill and keeps the job tracked (:2156-2220)."""
        with self._lock:
            match = _BACKGROUND_FILE.search(command)
            if match is not None:
                return self._background.get(match.group(1))
            match = _BACKGROUND_KILL.fullmatch(command.strip())
            if match is None:
                return None
            pid = int(match.group(1))
            owners = sorted(self._background_pids.get(pid, ()))
        if len(owners) > 1:
            raise RuntimeError(messages.ambiguous_pid(pid, owners))
        return owners[0] if owners else None

    def _record_background(self, session_id: str, target: str, result: dict) -> None:
        """A launch: remember where it runs and its pid, parsed as core parses it
        (process_registry.py:1291)."""
        lines = map(str.strip, (result.get("output") or "").splitlines())
        pid = next((int(line) for line in lines if line.isdigit()), None)
        with self._lock:
            self._background[session_id] = target
            if pid is not None:
                self._background_pids.setdefault(pid, set()).add(target)

    def _note(self, target: str) -> str:
        if target == targets.SERVER:
            return messages.TARGET_NOTE_SERVER
        device = store.get_device(target)
        if device is None:
            return messages.target_note(target, None, None)
        return messages.target_note(target, device["os"], device_path(device["home"]))

    def _command_cwd(self, target: str, cwd: str) -> str:
        """Where a terminal command or a background launch runs; "" keeps the target's own cwd. Core
        passes ``workdir=``, else the session's recorded cwd, else the configured default
        (terminal_tool.py:1014-1061, terminal_tool_background.py:173-176), and records the cwd only
        after a foreground terminal command (terminal_tool_result.py:222-225)."""
        from tools.approval import get_current_session_key
        from tools.terminal_tool import get_session_cwd

        session_key = get_current_session_key(default="")  # core's key (terminal_tool.py:1528-1530)
        recorded = get_session_cwd(session_key) if session_key else None
        if cwd == "~":
            # On the server core drops a /home or /Users record outside the live cwd, which a
            # workdir= command moves (terminal_tool.py:1041-1059); the record is still this host's.
            if (
                target == targets.SERVER
                and self._last_target == targets.SERVER
                and recorded
                and os.path.isdir(recorded)
            ):
                return recorded
            return ""
        if cwd == recorded:
            # It was recorded on the last terminal command's target; another target never saw it.
            return "" if self._last_target is not None and target != self._last_target else cwd
        if target != targets.SERVER and cwd == self._requested_cwd:
            return ""  # the configured default, a gateway directory
        return cwd

    # --- delegated surface -----------------------------------------------------------------
    def execute(self, command: str, cwd: str = "", **kwargs) -> dict:
        # Only the foreground terminal tool sets bounded_capture (base.py:679-681): its command runs on
        # this conversation's target exactly as approved, and only it moves the session's cwd record.
        terminal = bool(kwargs.get("bounded_capture"))
        launch = None if terminal else _BACKGROUND_LAUNCH.search(command)
        owner = None if terminal or launch else self._background_owner(command)
        if owner is not None:
            # The process registry polls and kills through this object (process_registry.py:1279,
            # 1418-1466, 2245-2246); its job lives on the sub-environment that launched it.
            result = self._sub(owner)[1].execute(command, cwd, **kwargs)
            if command.startswith("kill -0 ") and _hands_failure(result):
                # The liveness probe (:1460-1470) reads anything but "0" as an exit: a device that
                # is offline for a moment, or a relay restart, must not end the job's tracking.
                return {"output": "0", "returncode": 0}
            return result
        note = None
        try:
            # Every store read and write happens before the command runs: an error after it would
            # make the terminal tool retry, i.e. run the command again (terminal_tool.py:1363-1390).
            target, sub = self._resolve()
            # The file tools parse their raw output, so the note never goes there.
            if terminal and target != self._noted_target:
                note = self._note(target)
            session_key = get_session_env("HERMES_SESSION_KEY", "")
            if terminal and session_key and target != self._last_target:
                store.set_last_run(session_key, target)
        except sqlite3.Error:
            self._store_failed()
            return {"output": messages.STORE_DOWN, "returncode": UNREACHABLE}
        if terminal or launch is not None:
            cwd = self._command_cwd(target, cwd)
        if terminal:
            self._last_target = target
        if note is not None:
            self._noted_target = target
        result = sub.execute(command, cwd, **kwargs)
        if launch is not None:
            launched = _BACKGROUND_FILE.search(launch.group(1))
            if launched is not None:
                self._record_background(launched.group(1), target, result)
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
        """A workspace override (terminal_tool.py:358-362) names a gateway directory: devices keep theirs."""
        target, sub = self._current()
        if target == targets.SERVER:
            sub.cwd = value

    @property
    def env(self) -> dict:
        return self._current()[1].env

    @property
    def is_local(self) -> bool:
        try:
            return self._resolve()[1].is_local
        except sqlite3.Error:
            self._store_failed()
            return False

    @property
    def uses_host_filesystem(self) -> bool:
        """False while the store is unreadable, so the file tools go through :meth:`execute`, which
        refuses, instead of reading the gateway's own files (file_operations.py:912-922)."""
        try:
            return self._resolve()[1].uses_host_filesystem
        except sqlite3.Error:
            self._store_failed()
            return False

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


class ProbeEnvironment(BaseEnvironment):
    """What core's prompt-build backend probe gets (agent/prompt_builder.py:836-877). Run for real, it
    would start the session's device before the model has done anything, and its answer is cached
    for every session in the process (:880-895). It runs nothing, so core describes the backend with
    ``env_description`` instead."""

    def execute(self, command: str, cwd: str = "", **kwargs) -> dict:
        return {"output": "", "returncode": UNREACHABLE}

    def cleanup(self):
        """Nothing to release."""

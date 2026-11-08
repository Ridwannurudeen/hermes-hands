from __future__ import annotations

import secrets
import shlex
import unicodedata
from functools import partial

from . import messages, store, targets

_PROTECTED_TOOLS = frozenset({"terminal", "write_file", "patch", "execute_code"})
_READ_ONLY_ARGV = frozenset(
    {
        ("pwd",),
        ("whoami",),
        ("hostname",),
        ("uname", "-a"),
        ("df", "-h"),
        ("free", "-h"),
        ("ipconfig", "/all"),
        ("tasklist",),
    }
)
_READ_ONLY_EXECUTABLES = frozenset(argv[0] for argv in _READ_ONLY_ARGV)
_SCRIPT_EXECUTABLES = frozenset(
    {
        ".",
        "bash",
        "cmd",
        "cmd.exe",
        "cscript",
        "deno",
        "exec",
        "fish",
        "node",
        "perl",
        "php",
        "powershell",
        "pwsh",
        "py",
        "python",
        "python3",
        "ruby",
        "sh",
        "source",
        "wscript",
        "zsh",
    }
)
_SCRIPT_SUFFIXES = (
    ".bat",
    ".cmd",
    ".js",
    ".mjs",
    ".php",
    ".pl",
    ".ps1",
    ".py",
    ".pyw",
    ".rb",
    ".sh",
    ".vbs",
)
_SHELL_META = frozenset(";&|<>`$(){}*?[]~#!%")


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", partial(pre_tool_call, ctx))


def _witness_device(ctx) -> dict | None:
    from . import environment

    target = environment.resolve_current_target(ctx)
    if target == targets.SERVER:
        return None
    device = store.get_device(target)
    if device is None:
        return None
    device = store.device_by_id(device["device_id"])
    if device is None:
        raise RuntimeError("the selected device disappeared during target resolution")
    capabilities = device.get("capabilities")
    if (
        not device.get("witness")
        or device.get("session_state") != "active"
        or not isinstance(capabilities, list)
        or "witness-v1" not in capabilities
    ):
        return None
    return device


def _approval(device_name: str) -> dict:
    return {
        "action": "approve",
        "message": messages.WITNESS_APPROVAL.format(device=device_name),
        "rule_key": f"hands-witness:{secrets.token_hex(16)}",
    }


def _command_decision(command: str, device_name: str) -> dict | None:
    if not command.strip() or any(char in _SHELL_META for char in command):
        return {"action": "block", "message": messages.WITNESS_COMMAND_BLOCKED}
    if any(unicodedata.category(char) == "Cc" for char in command):
        return {"action": "block", "message": messages.WITNESS_COMMAND_BLOCKED}
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return {"action": "block", "message": messages.WITNESS_COMMAND_BLOCKED}
    if not argv:
        return {"action": "block", "message": messages.WITNESS_COMMAND_BLOCKED}
    executable = argv[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if (
        "/" in argv[0]
        or "\\" in argv[0]
        or executable in _SCRIPT_EXECUTABLES
        or executable.endswith(_SCRIPT_SUFFIXES)
    ):
        return {"action": "block", "message": messages.WITNESS_COMMAND_BLOCKED}
    if tuple(argv) in _READ_ONLY_ARGV:
        return None
    if executable in _READ_ONLY_EXECUTABLES:
        return {"action": "block", "message": messages.WITNESS_COMMAND_BLOCKED}
    return _approval(device_name)


def pre_tool_call(ctx, tool_name: str, args: dict, **kwargs):
    try:
        if tool_name not in _PROTECTED_TOOLS:
            return None
        device = _witness_device(ctx)
        if device is None:
            return None
        if tool_name == "execute_code":
            return {"action": "block", "message": messages.WITNESS_EXECUTE_CODE_BLOCKED}
        if not isinstance(args, dict):
            return {"action": "block", "message": messages.WITNESS_COMMAND_BLOCKED}
        if tool_name in {"write_file", "patch"}:
            return _approval(device["name"])
        command = args.get("command")
        if not isinstance(command, str):
            return {"action": "block", "message": messages.WITNESS_COMMAND_BLOCKED}
        return _command_decision(command, device["name"])
    except Exception:
        if isinstance(tool_name, str) and tool_name in _PROTECTED_TOOLS:
            return {"action": "block", "message": messages.WITNESS_TARGET_BLOCKED}
        return None

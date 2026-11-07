from __future__ import annotations

import json
import os
import re
import sys

from . import config
from .protocol import canonical_json, policy_hash

DEFAULTS = [
    r"\bhermes-hands\b",
    r"\.hermes-hands(?:[/\\]|\b)",
    r"\b(?:taskkill|Stop-Process|kill)\b.*(?:hermes-hands|\$PPID|\b(?:PID|pid)\b)",
    r"\b(?:schtasks\s+/(?:End|Delete)|Stop-ScheduledTask|Unregister-ScheduledTask)\b.*HermesHands",
    r"\bsudo\b",
    r"(?:rm\s+-rf\s+(?:/(?:\s|$)|~(?:[/\s]|$))|\bRemove-Item\b.*(?:[A-Za-z]:\\|[/\\])(?:\s|$).*?-Recurse)",
    r"\bmkfs(?:\.[\w-]+)?\b|\bdd\b.*\bof=/dev/",
    r"\bdiskpart\b|\bformat\b.*(?:[A-Za-z]:|/FS:)",
    r"\b(?:shutdown|reboot|Stop-Computer)\b",
    r"(?:curl|wget|Invoke-WebRequest).*\|\s*(?:sh|bash|pwsh|powershell)",
    r"(?:taskkill\s+/PID\s+<agent-pid>\b|Stop-Process\b.*-Id\s+<agent-pid>\b|\bkill\s+-?9?\s+<agent-pid>\b)",
]


def patterns() -> list[str]:
    if config.POLICY_PATH.exists():
        deny = json.loads(config.POLICY_PATH.read_text(encoding="utf-8-sig"))["deny"]
    else:
        deny = DEFAULTS.copy()
    return deny


def reset() -> str:
    config.ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    raw = canonical_json({"deny": DEFAULTS})
    config.POLICY_PATH.write_bytes(raw)
    if sys.platform != "win32":
        config.POLICY_PATH.chmod(0o600)
    return policy_hash(raw)


def digest() -> str:
    if config.POLICY_PATH.exists():
        return policy_hash(config.POLICY_PATH.read_bytes())
    return policy_hash(canonical_json({"deny": patterns()}))


def refusal(script: str) -> str | None:
    for pattern in patterns():
        active_pattern = pattern.replace("<agent-pid>", str(os.getpid()))
        if re.search(active_pattern, script, re.IGNORECASE):
            return pattern
    return None


def check(script: str) -> int:
    pattern = refusal(script)
    if pattern:
        print(f"Policy refused command matching: {pattern}")
        return 126
    return 0


def show() -> None:
    for item in patterns():
        print(item)

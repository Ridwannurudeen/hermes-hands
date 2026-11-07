from __future__ import annotations

import shutil
import subprocess
import sys

TASK_NAME = "HermesHands"


def command(*, executable: str, remove: bool = False) -> list[str]:
    if remove:
        return ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
    task_run = subprocess.list2cmdline([executable, "serve"])
    return ["schtasks", "/Create", "/SC", "ONLOGON", "/RL", "LIMITED", "/TN", TASK_NAME, "/TR", task_run, "/F"]


def install(*, dry_run: bool = False, remove: bool = False) -> int:
    if sys.platform != "win32":
        print("install-service is not supported in v0.1 on this platform", file=sys.stderr)
        return 1
    if remove:
        args = command(executable="", remove=True)
    else:
        executable = shutil.which("hermes-hands")
        if not executable:
            print("hermes-hands executable was not found on PATH", file=sys.stderr)
            return 1
        args = command(executable=executable)
    print(subprocess.list2cmdline(args))
    if dry_run:
        return 0
    result = subprocess.run(args, check=False)
    return result.returncode

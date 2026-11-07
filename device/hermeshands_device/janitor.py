from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)
MAX_AGE_SECONDS = 24 * 60 * 60
SWEEP_INTERVAL_SECONDS = 60 * 60


def _temp_dirs() -> set[Path]:
    candidates: set[Path] = set()
    if os.name == "nt":
        for name in ("TERMINAL_TEMP_DIR", "TMPDIR"):
            value = os.environ.get(name)
            if value and os.path.isabs(value) and os.path.isdir(value):
                candidates.add(Path(value))
        hermes_home = os.environ.get("HERMES_HOME")
        if hermes_home:
            candidates.add(Path(hermes_home) / "cache" / "terminal")
        else:
            local_app_data = os.environ.get("LOCALAPPDATA")
            home = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
            candidates.add(home / "hermes" / "cache" / "terminal")
        candidates.add(Path(tempfile.gettempdir()))
    else:
        for name in ("TERMINAL_TEMP_DIR", "TMPDIR", "TMP", "TEMP"):
            value = os.environ.get(name)
            if value and value.startswith("/"):
                candidates.add(Path(value))
        hermes_home = os.environ.get("HERMES_HOME")
        candidates.add((Path(hermes_home) if hermes_home else Path.home() / ".hermes") / "cache" / "terminal")
        candidates.add(Path(tempfile.gettempdir()))
    return candidates


def _matches(name: str) -> bool:
    return name.startswith("hermes-snap-") and name.endswith(".sh") or name.startswith("hermes-cwd-") and name.endswith(".txt") or name.startswith("tmp") and name.endswith(".sh")


def cleanup(now: float | None = None) -> int:
    cutoff = (time.time() if now is None else now) - MAX_AGE_SECONDS
    removed = 0
    for directory in _temp_dirs() | {config.OUT_DIR}:
        if directory.is_symlink():
            continue
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
        with entries:
            for entry in entries:
                if not _matches(entry.name) or not entry.is_file(follow_symlinks=False):
                    continue
                try:
                    if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                        Path(entry.path).unlink()
                        removed += 1
                except OSError:
                    continue
    logger.info("Device temp janitor removed %d files", removed)
    return removed


def start() -> None:
    logging.basicConfig(level=logging.INFO)
    cleanup()

    def sweep_hourly() -> None:
        while True:
            time.sleep(SWEEP_INTERVAL_SECONDS)
            cleanup()

    threading.Thread(target=sweep_hourly, name="device-temp-janitor", daemon=True).start()

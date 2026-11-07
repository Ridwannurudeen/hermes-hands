"""Resolve the execution target for one terminal call."""

from . import store

SERVER = "server"


def resolve_target(*, session_key: str, in_cron: bool, default_target: str) -> str:
    if in_cron:
        return SERVER
    if session_key:
        target = store.get_session_target(session_key)
        if target is not None:
            return target
    return default_target

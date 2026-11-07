"""``HandsProvider``: the ``hands`` terminal backend (``agent/terminal_env_provider.py:20-122``).

``is_container=True`` gives the file tools sandbox path semantics (pure POSIX, no host ``resolve()``),
which a Windows device needs; ``skip_container_guards`` is overridden to False so Hermes's
dangerous-command approvals stay on. ``is_available`` is always True: the ``server`` target never needs
the relay, and a device target fails closed per call.
"""

from __future__ import annotations

from typing import Any, Optional

from agent.terminal_env_provider import TerminalEnvironmentProvider

from .environment import HandsEnvironment


class HandsProvider(TerminalEnvironmentProvider):
    name = "hands"
    display_name = "Hermes Hands"
    is_remote = True
    is_container = True
    supports_streams = False

    def __init__(self, ctx):
        self._ctx = ctx

    @property
    def skip_container_guards(self) -> bool:
        return False

    @property
    def default_cwd(self) -> Optional[str]:
        return "~"

    @property
    def env_description(self) -> str:
        return "a device paired with Hermes Hands, or the gateway host for the server target"

    def is_available(self) -> bool:
        return True

    def create_environment(
        self,
        *,
        cwd: str,
        timeout: int,
        task_id: str = "default",
        image: Optional[str] = None,
        container_config: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> HandsEnvironment:
        return HandsEnvironment(self._ctx, cwd=cwd, timeout=timeout)

"""Owner-gated Hands slash commands."""

from __future__ import annotations

import os
import shlex

from . import client, messages, store, targets


def _ids(value: object) -> set[str]:
    if isinstance(value, (list, tuple, set, frozenset)):
        items = value
    elif isinstance(value, str):
        items = value.split(",")
    elif value is None:
        items = []
    else:
        items = [value]
    return {str(item).strip() for item in items if str(item).strip()}


def _platform(platform: str, source) -> str:
    value = getattr(source, "platform", None) or platform
    return str(getattr(value, "value", value) or "").lower()


def _in_shared_chat(source) -> bool:
    """A group, channel or thread: core gives its participants one session (session.py:690-725).
    Slack reports a group DM (MPIM) as chat_type dm too (slack/adapter.py:4597, 4698); only a 1:1
    IM has a channel id that starts with D (slack/adapter.py:5463)."""
    chat_type = str(getattr(source, "chat_type", "dm") or "dm").strip().lower()
    if chat_type not in {"", "dm", "direct", "private"}:
        return True
    return _platform("", source) == "slack" and not str(getattr(source, "chat_id", "") or "").startswith("D")


def _caller(platform: str, source) -> str:
    """This command's sender, named as ``targets.current_caller`` names the sender of a tool call."""
    return targets.caller_id(_platform(platform, source), str(getattr(source, "user_id", "") or ""))


def terminal_backend() -> str:
    """The terminal backend Hermes resolves for this profile: config.yaml terminal.backend, else
    TERMINAL_ENV from the profile .env or this process's environment (terminal_scope.py:114)."""
    from hermes_constants import get_hermes_home
    from tools.terminal_scope import build_profile_terminal_scope

    launch = {key: value for key, value in os.environ.items() if key.startswith("TERMINAL_")}
    return build_profile_terminal_scope(get_hermes_home(), env_overlay=launch).get("TERMINAL_ENV", "")


def _is_owner(ctx, platform: str, source) -> bool:
    if source is None and platform in {"cli", "tui", "desktop"}:
        return True
    source_platform = getattr(source, "platform", None)
    if str(getattr(source_platform, "value", source_platform)) == "local":
        return True
    user_id = getattr(source, "user_id", None)
    if not user_id:
        return False
    owners = _ids(ctx.get_config("owner_ids", default=[]))
    if owners:
        return f"{platform}:{user_id}" in owners
    try:
        from gateway.config import load_gateway_config

        config = load_gateway_config()
        platform_config = next(
            (value for key, value in config.platforms.items() if str(getattr(key, "value", key)) == platform),
            None,
        )
        extra = getattr(platform_config, "extra", {}) if platform_config else {}
        allowed = _ids(extra.get("group_allow_admin_from" if _in_shared_chat(source) else "allow_admin_from"))
        return str(user_id) in allowed
    except Exception:
        return False


def register_commands(ctx, *, has_seed: bool) -> None:
    def handle(raw_args: str, *, session_id="", session_key="", platform="", source=None) -> str:
        try:
            args = shlex.split(raw_args)
        except ValueError:
            return "Usage: /hands [status|pair|use|devices|revoke|accept-policy|setup]"
        action = args[0].lower() if args else "status"
        if action == "status":
            devices = store.list_devices()
            relay, cause = "up", []
            if not store.relay_alive():
                try:
                    client.state()
                except client.HandsError as exc:
                    relay, cause = "down", [str(exc)]
                else:
                    if not store.relay_alive():  # the relay started, so it wrote its heartbeat
                        relay, cause = "down", [messages.STORE_SPLIT]
            target = targets.target_for(
                session_key=session_key,
                caller=_caller(platform, source),
                default_target=str(ctx.get_config("default_target", default=targets.SERVER)),
            )
            status = f"Hands: relay {relay}; target {target}; {len(devices)} device(s)."
            alerts = messages.alerts(devices)
            return "\n".join([status, *cause, *alerts])
        if action == "devices" and len(args) == 1:
            devices = store.list_devices()
            if not devices:
                return "No paired devices."
            return "\n".join(f"{d['name']}: {'online' if d['online'] else 'offline'}" for d in devices)
        if action not in {"pair", "use", "revoke", "accept-policy", "setup"}:
            return "Usage: /hands [status|pair|use|devices|revoke|accept-policy|setup]"
        if not _is_owner(ctx, platform, source):
            return "Hands: owner access required."
        if action == "setup" and len(args) == 1:
            user_id = getattr(source, "user_id", None)
            owner_id = f"{platform}:{user_id}" if user_id else "local owner"
            return (
                f"Owner: {owner_id}\n"
                "Select the Hands backend with: hermes config set terminal.backend hands\n"
                "Set desktop placement with: hermes config set bot_desktop.placement gateway\n"
                "Then restart the gateway and the dashboard."
            )
        if len(args) != 2:
            return f"Usage: /hands {action} <name>"
        name = args[1]
        if action == "pair":
            if _in_shared_chat(source):
                return messages.PAIR_DIRECT_ONLY
            if name == store.RESERVED_NAME:
                return messages.PAIR_NAME_RESERVED
            if not has_seed:
                return messages.NOT_SET_UP
            code = store.mint_pairing_code(name)
            from hermes_cli.config import load_config_readonly

            dashboard = (load_config_readonly() or {}).get("dashboard") or {}
            gateway_url = (
                dashboard.get("public_url")
                or ctx.get_config("dashboard_url", default="http://127.0.0.1:9119")
            )
            command = f"hermes-hands pair {gateway_url} --name {shlex.quote(name)} --code={shlex.quote(code)}"
            return f"Pair code: {code}\nRun on the device: {command}"
        if action == "use":
            if name != "server" and _in_shared_chat(source):
                return messages.USE_DIRECT_ONLY
            backend = terminal_backend()
            if name != "server" and backend != "hands":
                return (
                    f"Hands: terminal.backend is {backend or 'not set'}, not hands, so commands would still run on "
                    "this host. Run hermes config set terminal.backend hands on the gateway host, then restart "
                    "the gateway."
                )
            device = store.get_device(name)
            if name != "server" and device is None:
                return f"No paired device named {name}."
            if not session_key:
                return "Hands: no session key is available."
            store.set_session_target(session_key, name, owner=_caller(platform, source))
            targets.notify_target_changed(session_key)
            return f"Target set to {name}; it applies from the next command."
        if action == "revoke":
            answer = ctx.ask_choice(f"Revoke {name}?", [{"value": "yes", "label": "Yes"}, {"value": "no", "label": "No"}])
            if answer != "yes":
                return "Revoke cancelled."
            return "Device revoked." if store.revoke(name) else f"No paired device named {name}."
        return "Policy accepted." if store.accept_policy(name) else f"No paired device named {name}."

    ctx.register_command("hands", handle, description="Pair and manage Hands devices", args_hint="[action]", argument_mode="mixed")

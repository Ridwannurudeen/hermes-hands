"""Owner-gated Hands slash commands."""

from __future__ import annotations

import shlex

from . import store


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


def _is_owner(ctx, platform: str, source) -> bool:
    from gateway.session import Platform

    if source is None and platform in {"cli", "tui", "desktop"}:
        return True
    source_platform = getattr(source, "platform", None)
    if source_platform == Platform.LOCAL:
        return True
    user_id = getattr(source, "user_id", None)
    if not user_id:
        return False
    if f"{platform}:{user_id}" in _ids(ctx.get_config("owner_ids", default=[])):
        return True
    try:
        from gateway.config import load_gateway_config

        config = load_gateway_config()
        platform_key = Platform(platform)
        platform_config = config.platforms.get(platform_key)
        extra = getattr(platform_config, "extra", {}) if platform_config else {}
        is_group = str(getattr(source, "chat_type", "dm") or "dm").strip().lower() not in {
            "", "dm", "direct", "private"
        }
        allowed = _ids(extra.get("group_allow_admin_from" if is_group else "allow_admin_from"))
        return str(user_id) in allowed
    except Exception:
        return False


def register_commands(ctx) -> None:
    def handle(raw_args: str, *, session_id="", session_key="", platform="", source=None) -> str:
        try:
            args = shlex.split(raw_args)
        except ValueError:
            return "Usage: /hands [status|pair|use|devices|revoke|accept-policy|setup]"
        action = args[0].lower() if args else "status"
        if action == "status":
            devices = store.list_devices()
            relay = "up" if store.relay_alive() else "down"
            target = store.get_session_target(session_key) if session_key else None
            return f"Hands: relay {relay}; target {target or ctx.get_config('default_target', default='server')}; {len(devices)} device(s)."
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
            return f"Owner: {owner_id}\nSet desktop placement with: hermes config set bot_desktop.placement gateway"
        if len(args) != 2:
            return f"Usage: /hands {action} <name>"
        name = args[1]
        if action == "pair":
            code = store.mint_pairing_code(name)
            from hermes_cli.config import load_config_readonly

            dashboard = (load_config_readonly() or {}).get("dashboard") or {}
            gateway_url = (
                ctx.get_config("public_url", default="")
                or dashboard.get("public_url")
                or ctx.get_config("dashboard_url", default="http://127.0.0.1:9119")
            )
            command = f"hermes-hands pair {gateway_url} --name {shlex.quote(name)} --code {shlex.quote(code)}"
            return f"Pair code: {code}\nRun on the device: {command}"
        if action == "use":
            device = store.get_device(name)
            if name != "server" and device is None:
                return f"No paired device named {name}."
            if not session_key:
                return "Hands: no session key is available."
            store.set_session_target(session_key, name)
            return f"Target set to {name}; it applies from the next command."
        if action == "revoke":
            answer = ctx.ask_choice(f"Revoke {name}?", [{"value": "yes", "label": "Yes"}, {"value": "no", "label": "No"}])
            if answer != "yes":
                return "Revoke cancelled."
            return "Device revoked." if store.revoke(name) else f"No paired device named {name}."
        return "Policy accepted." if store.accept_policy(name) else f"No paired device named {name}."

    ctx.register_command("hands", handle, description="Pair and manage Hands devices", args_hint="[action]", argument_mode="mixed")

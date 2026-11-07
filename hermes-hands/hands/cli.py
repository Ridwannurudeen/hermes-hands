"""Hermes CLI commands for the Hands gateway plugin."""

from __future__ import annotations

import secrets

from . import store


def register_cli(ctx) -> None:
    def setup_parser(parser):
        commands = parser.add_subparsers(dest="hands_action", required=True)
        commands.add_parser("status")
        commands.add_parser("devices")
        revoke = commands.add_parser("revoke")
        revoke.add_argument("name")
        revoke.add_argument("--yes", action="store_true")
        commands.add_parser("setup")
        commands.add_parser("doctor")

    def handler(args):
        action = args.hands_action
        if action == "status":
            print(f"Relay: {'up' if store.relay_alive() else 'down'}")
            for device in store.list_devices():
                print(f"{device['name']}: {'online' if device['online'] else 'offline'}")
            return
        if action == "devices":
            for device in store.list_devices():
                print(f"{device['name']}: {'online' if device['online'] else 'offline'}")
            return
        if action == "revoke":
            if not args.yes and input(f"Revoke {args.name}? [y/N] ").strip().lower() != "y":
                print("Revoke cancelled.")
                return
            print("Device revoked." if store.revoke(args.name) else f"No paired device named {args.name}.")
            return
        if action == "setup":
            from agent.secret_scope import get_secret
            from hermes_cli.gateway_setup_wizard import save_env_values

            if not get_secret("HANDS_RELAY_SEED"):
                save_env_values(HANDS_RELAY_SEED=secrets.token_hex(32))
                print("Relay seed saved. Restart the gateway and dashboard.")
            else:
                print("Relay seed is already set.")
            owner_id = input("Owner Telegram user id: ").strip()
            if owner_id:
                ctx.set_config("owner_ids", [f"telegram:{owner_id}"])
                print(f"Owner saved: telegram:{owner_id}")
            else:
                print("Owner id was not saved.")
            from tools.bot_desktop import placement

            result = placement.resolve()
            if result.where == placement.REFUSED:
                print(result.reason)
                print("hermes config set bot_desktop.placement gateway")
            return
        if action == "doctor":
            from hermes_cli.config import load_config_readonly
            from hermes_cli.dashboard_auth.registry import list_interactive_providers
            from hermes_cli.dashboard_procs import _scan_dashboard_processes
            from hermes_cli.main_dashboard import _dashboard_listening, _parse_dashboard_runtime

            cfg = load_config_readonly() or {}
            dashboard = cfg.get("dashboard") or {}
            public_url = str(dashboard.get("public_url") or "").strip()
            runtime = next(
                (
                    parsed
                    for _pid, command in _scan_dashboard_processes()
                    if (parsed := _parse_dashboard_runtime(command)) is not None
                    and parsed[0] == "dashboard"
                ),
                None,
            )
            bind_ok = bool(
                runtime
                and runtime[1] in {"127.0.0.1", "localhost", "::1"}
                and _dashboard_listening(runtime[1], runtime[2])
            )
            _check("Dashboard loopback bind", bind_ok, "Bind the dashboard to 127.0.0.1.")
            _check("dashboard.public_url", bool(public_url), "Set dashboard.public_url to the public HTTPS URL.")
            _check("Interactive dashboard auth", bool(list_interactive_providers()), "Configure dashboard.basic_auth or another interactive auth provider.")
            try:
                from . import client

                reachable = bool(client.state().get("relay"))
            except Exception:
                reachable = False
            _check("Relay reachable", reachable, "Start the Hermes dashboard hosting the Hands relay.")
            _check("Shared plugin_data_dir", store.relay_alive(), "Run the gateway and dashboard with the same HERMES_HOME.")

    def _check(label: str, passed: bool, fix: str) -> None:
        print(f"{'PASS' if passed else 'FAIL'} {label}" + ("" if passed else f" — {fix}"))

    ctx.register_cli_command("hands", "Manage Hermes Hands", setup_parser, handler)

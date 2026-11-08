"""Hermes CLI commands for the Hands gateway plugin."""

from __future__ import annotations

import secrets
import shlex

from . import client, commands, messages, store


def register_cli(ctx, *, has_seed: bool) -> None:
    def setup_parser(parser):
        commands = parser.add_subparsers(dest="hands_action", required=True)
        commands.add_parser("status")
        commands.add_parser("devices")
        revoke = commands.add_parser("revoke")
        revoke.add_argument("name")
        revoke.add_argument("--yes", action="store_true")
        commands.add_parser("setup")
        commands.add_parser("doctor")
        pair = commands.add_parser("pair")
        pair.add_argument("name")

    def handler(args):
        action = args.hands_action
        if action == "status":
            if store.relay_alive():
                print("Relay: up")
            else:
                try:
                    client.state()
                except client.HandsError as exc:
                    print(f"Relay: down\n{exc}")
                else:  # the relay started, so it wrote its heartbeat
                    print("Relay: up" if store.relay_alive() else f"Relay: down\n{messages.STORE_SPLIT}")
            for device in store.list_devices():
                print(f"{device['name']}: {'online' if device['online'] else 'offline'}")
            return
        if action == "devices":
            for device in store.list_devices():
                print(f"{device['name']}: {'online' if device['online'] else 'offline'}")
            return
        if action == "pair":
            if not has_seed:
                print(messages.NOT_SET_UP)
                return
            from hermes_cli.config import load_config_readonly

            name = args.name
            code = store.mint_pairing_code(name)
            dashboard = (load_config_readonly() or {}).get("dashboard") or {}
            gateway_url = (
                dashboard.get("public_url")
                or ctx.get_config("dashboard_url", default="http://127.0.0.1:9119")
            )
            command = f"hermes-hands pair {gateway_url} --name {shlex.quote(name)} --code={shlex.quote(code)}"
            print(f"Pair code: {code}\nRun on the device: {command}")
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
            # Before the prompt, so every run shows them, an aborted prompt included.
            print("Select the Hands backend with: hermes config set terminal.backend hands")
            print("Set desktop placement with: hermes config set bot_desktop.placement gateway")
            print("Then restart the gateway and the dashboard.")
            owner_id = input("Owner Telegram user id: ").strip()
            if owner_id:
                ctx.set_config("owner_ids", [f"telegram:{owner_id}"])
                print(f"Owner saved: telegram:{owner_id}")
            else:
                print("Owner id was not saved.")
            return
        if action == "doctor":
            from hermes_cli.config import load_config_readonly
            from hermes_cli.dashboard_auth import list_interactive_providers

            cfg = load_config_readonly() or {}
            backend = commands.terminal_backend()
            dashboard = cfg.get("dashboard") or {}
            public_url = str(dashboard.get("public_url") or "").strip()
            _check(
                "terminal.backend is hands",
                backend == "hands",
                f"It is {backend or 'not set'}. Run hermes config set terminal.backend hands, then restart the gateway.",
            )
            # /local/state proves the route is mounted, the seed matches and the relay answers; it
            # also starts the relay, which writes its first heartbeat row before it replies.
            try:
                client.state()
                relay_error = ""
            except client.HandsError as exc:
                relay_error = str(exc)
            _check("Relay answers at dashboard_url", not relay_error, relay_error)
            _check("dashboard.public_url", bool(public_url), "Set dashboard.public_url to the public HTTPS URL of the Hands host.")
            _check("Interactive dashboard auth", bool(list_interactive_providers()), "Configure dashboard.basic_auth or another interactive auth provider.")
            _check(
                "Shared plugin_data_dir",
                store.relay_alive(),
                "Fix the relay check first." if relay_error else "Run the gateway and dashboard with the same HERMES_HOME.",
            )

    def _check(label: str, passed: bool, fix: str) -> None:
        print(f"{'PASS' if passed else 'FAIL'} {label}" + ("" if passed else f": {fix}"))

    ctx.register_cli_command("hands", "Manage Hermes Hands", setup_parser, handler)

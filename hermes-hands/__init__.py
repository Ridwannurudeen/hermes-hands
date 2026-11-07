"""Hermes Hands: your own laptop as Hermes's hands. See plugin.yaml and README.md."""

from __future__ import annotations


def register(ctx) -> None:
    """Load-time registration. The relay seed is read here, once, under the plugin manager's home
    scope; request handlers and the lazily started relay never call ``get_secret``."""
    from agent.secret_scope import get_secret
    from hermes_cli.dashboard_auth.token_auth import register_token_route

    from .hands import auth, cli, client, commands, relay, store, tools
    from .hands.provider import HandsProvider

    store.OFFLINE_AFTER = int(ctx.get_config("offline_after_seconds", 20))
    seed = get_secret("HANDS_RELAY_SEED")
    keys = relay.derive_keys(seed) if seed else None
    client.configure(
        str(ctx.get_config("dashboard_url", "http://127.0.0.1:9119")),
        keys.local_bearer if keys is not None else None,
    )
    ctx.register_terminal_environment_provider(HandsProvider(ctx))
    ctx.register_dashboard_auth_provider(auth.HandsTokenProvider(keys))
    for path in relay.TOKEN_ROUTES:
        register_token_route(path)
    commands.register_commands(ctx)
    cli.register_cli(ctx)
    tools.register_tools(ctx)

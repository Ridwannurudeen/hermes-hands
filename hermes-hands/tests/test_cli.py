import sys
import types

from hands import cli


class Context:
    def __init__(self):
        self.config = {}
        self.set_calls = []

    def register_cli_command(self, name, help, setup_fn, handler_fn):
        self.setup_fn = setup_fn
        self.handler = handler_fn

    def set_config(self, key, value):
        self.set_calls.append((key, value))

    def get_config(self, key, default=None):
        return self.config.get(key, default)


def test_setup_writes_seed_once_and_owner(monkeypatch, capsys):
    calls = []
    secret = {"value": None}
    secret_scope = types.ModuleType("agent.secret_scope")
    secret_scope.get_secret = lambda key: secret["value"]
    wizard = types.ModuleType("hermes_cli.gateway_setup_wizard")

    def save_env_values(**values):
        calls.append(values)
        secret["value"] = values["HANDS_RELAY_SEED"]

    wizard.save_env_values = save_env_values
    placement = types.ModuleType("tools.bot_desktop.placement")
    placement.REFUSED = "refused"
    placement.resolve = lambda: types.SimpleNamespace(where="refused", reason="Set gateway placement.")
    tools = types.ModuleType("tools")
    tools.__path__ = []
    bot_desktop = types.ModuleType("tools.bot_desktop")
    bot_desktop.__path__ = []
    bot_desktop.placement = placement
    agent = types.ModuleType("agent")
    agent.__path__ = []
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", secret_scope)
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.gateway_setup_wizard", wizard)
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.bot_desktop", bot_desktop)
    monkeypatch.setitem(sys.modules, "tools.bot_desktop.placement", placement)
    monkeypatch.setattr("builtins.input", lambda prompt: "12345")
    ctx = Context()
    cli.register_cli(ctx)
    args = types.SimpleNamespace(hands_action="setup")
    ctx.handler(args)
    ctx.handler(args)
    assert len(calls) == 1
    assert len(calls[0]["HANDS_RELAY_SEED"]) == 64
    assert ctx.set_calls == [("owner_ids", ["telegram:12345"])] * 2
    assert "hermes config set bot_desktop.placement gateway" in capsys.readouterr().out


def test_doctor_reports_each_deployment_check(monkeypatch, capsys):
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    config = types.ModuleType("hermes_cli.config")
    config.load_config_readonly = lambda: {"dashboard": {"public_url": "https://hands.example"}}
    auth = types.ModuleType("hermes_cli.dashboard_auth")
    auth.__path__ = []
    registry = types.ModuleType("hermes_cli.dashboard_auth.registry")
    registry.list_interactive_providers = lambda: [object()]
    procs = types.ModuleType("hermes_cli.dashboard_procs")
    procs._scan_dashboard_processes = lambda: [(1, "dashboard --host 127.0.0.1")]
    dashboard = types.ModuleType("hermes_cli.main_dashboard")
    dashboard._parse_dashboard_runtime = lambda command: ("dashboard", "127.0.0.1", 9119)
    dashboard._dashboard_listening = lambda host, port: True
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config)
    monkeypatch.setitem(sys.modules, "hermes_cli.dashboard_auth", auth)
    monkeypatch.setitem(sys.modules, "hermes_cli.dashboard_auth.registry", registry)
    monkeypatch.setitem(sys.modules, "hermes_cli.dashboard_procs", procs)
    monkeypatch.setitem(sys.modules, "hermes_cli.main_dashboard", dashboard)
    monkeypatch.setattr(cli.store, "relay_alive", lambda: True)
    client = types.ModuleType("hands.client")
    client.state = lambda: {"relay": True}
    monkeypatch.setitem(sys.modules, "hands.client", client)
    import hands

    monkeypatch.setattr(hands, "client", client, raising=False)
    ctx = Context()
    cli.register_cli(ctx)
    ctx.handler(types.SimpleNamespace(hands_action="doctor"))
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 5
    assert all(line.startswith("PASS ") for line in lines)

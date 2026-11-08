import argparse
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
    agent = types.ModuleType("agent")
    agent.__path__ = []
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", secret_scope)
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.gateway_setup_wizard", wizard)
    monkeypatch.setattr("builtins.input", lambda prompt: "12345")
    ctx = Context()
    cli.register_cli(ctx, has_seed=True)
    args = types.SimpleNamespace(hands_action="setup")
    ctx.handler(args)
    ctx.handler(args)
    assert len(calls) == 1
    assert len(calls[0]["HANDS_RELAY_SEED"]) == 64
    assert ctx.set_calls == [("owner_ids", ["telegram:12345"])] * 2
    out = capsys.readouterr().out
    assert "hermes config set terminal.backend hands" in out
    assert "hermes config set bot_desktop.placement gateway" in out
    assert "restart the gateway" in out


def test_setup_prints_the_backend_and_placement_steps_even_if_the_prompt_is_aborted(
    monkeypatch, capsys
):
    secret_scope = types.ModuleType("agent.secret_scope")
    secret_scope.get_secret = lambda key: "seed"
    agent = types.ModuleType("agent")
    agent.__path__ = []
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    wizard = types.ModuleType("hermes_cli.gateway_setup_wizard")
    wizard.save_env_values = lambda **values: None
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", secret_scope)
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.gateway_setup_wizard", wizard)

    def aborted(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", aborted)
    ctx = Context()
    cli.register_cli(ctx, has_seed=True)
    try:
        ctx.handler(types.SimpleNamespace(hands_action="setup"))
    except EOFError:
        pass
    out = capsys.readouterr().out
    assert "Relay seed is already set." in out
    assert "hermes config set terminal.backend hands" in out
    assert "hermes config set bot_desktop.placement gateway" in out


def install_doctor_modules(monkeypatch, config):
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    config_module = types.ModuleType("hermes_cli.config")
    config_module.load_config_readonly = lambda: config
    auth = types.ModuleType("hermes_cli.dashboard_auth")
    auth.list_interactive_providers = lambda: [object()]
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config_module)
    monkeypatch.setitem(sys.modules, "hermes_cli.dashboard_auth", auth)
    backend = (config.get("terminal") or {}).get("backend") or ""
    monkeypatch.setattr(cli.commands, "terminal_backend", lambda: backend)


def run_doctor(capsys):
    ctx = Context()
    cli.register_cli(ctx, has_seed=True)
    ctx.handler(types.SimpleNamespace(hands_action="doctor"))
    return capsys.readouterr().out.splitlines()


def test_doctor_reports_each_deployment_check(monkeypatch, capsys):
    install_doctor_modules(
        monkeypatch, {"terminal": {"backend": "hands"}, "dashboard": {"public_url": "https://hands.example"}}
    )
    monkeypatch.setattr(cli.client, "state", lambda: {"relay": True, "devices": [], "alerts": []})
    monkeypatch.setattr(cli.store, "relay_alive", lambda: True)
    lines = run_doctor(capsys)
    assert len(lines) == 5
    assert all(line.startswith("PASS ") for line in lines)


def test_doctor_names_the_backend_and_the_relay_refusal(monkeypatch, capsys):
    install_doctor_modules(
        monkeypatch, {"terminal": {"backend": "local"}, "dashboard": {"public_url": "https://hands.example"}}
    )

    def refuse():
        raise cli.client.HandsError("hands: the relay is not reachable.")

    monkeypatch.setattr(cli.client, "state", refuse)
    monkeypatch.setattr(cli.store, "relay_alive", lambda: False)
    lines = run_doctor(capsys)
    assert lines[0] == (
        "FAIL terminal.backend is hands: It is local. Run hermes config set terminal.backend hands, "
        "then restart the gateway."
    )
    assert lines[1] == "FAIL Relay answers at dashboard_url: hands: the relay is not reachable."
    assert lines[4] == "FAIL Shared plugin_data_dir: Fix the relay check first."


def test_doctor_reads_the_heartbeat_only_after_the_relay_answered(monkeypatch, capsys):
    """/local/state starts the relay, which writes its first heartbeat row before replying."""
    install_doctor_modules(
        monkeypatch, {"terminal": {"backend": "hands"}, "dashboard": {"public_url": "https://hands.example"}}
    )
    calls = []
    monkeypatch.setattr(cli.client, "state", lambda: calls.append("state") or {"relay": True})
    monkeypatch.setattr(cli.store, "relay_alive", lambda: calls.append("heartbeat") or "state" in calls)
    assert run_doctor(capsys)[4] == "PASS Shared plugin_data_dir"
    assert calls == ["state", "heartbeat"]


def test_doctor_blames_hermes_home_only_when_the_relay_answers(monkeypatch, capsys):
    install_doctor_modules(
        monkeypatch, {"terminal": {"backend": "hands"}, "dashboard": {"public_url": "https://hands.example"}}
    )
    monkeypatch.setattr(cli.client, "state", lambda: {"relay": True, "devices": [], "alerts": []})
    monkeypatch.setattr(cli.store, "relay_alive", lambda: False)
    assert run_doctor(capsys)[4] == (
        "FAIL Shared plugin_data_dir: Run the gateway and dashboard with the same HERMES_HOME."
    )


def test_status_asks_a_relay_that_has_no_fresh_heartbeat(monkeypatch, capsys):
    alive = []  # the relay writes its heartbeat before it answers /local/state
    monkeypatch.setattr(cli.store, "relay_alive", lambda: bool(alive))
    monkeypatch.setattr(cli.store, "list_devices", lambda: [])
    monkeypatch.setattr(
        cli.client, "state", lambda: alive.append(True) or {"relay": True, "devices": [], "alerts": []}
    )
    ctx = Context()
    cli.register_cli(ctx, has_seed=True)
    ctx.handler(types.SimpleNamespace(hands_action="status"))
    assert capsys.readouterr().out == "Relay: up\n"
    alive.clear()
    monkeypatch.setattr(cli.client, "state", lambda: {"relay": True, "devices": [], "alerts": []})
    ctx.handler(types.SimpleNamespace(hands_action="status"))  # V50: it answers, its row is elsewhere
    assert capsys.readouterr().out == f"Relay: down\n{cli.messages.STORE_SPLIT}\n"

    def refuse():
        raise cli.client.HandsError("hands: the relay is not reachable.")

    monkeypatch.setattr(cli.client, "state", refuse)
    ctx.handler(types.SimpleNamespace(hands_action="status"))
    assert capsys.readouterr().out == "Relay: down\nhands: the relay is not reachable.\n"


def test_pair_cli_mints_code_and_prints_device_command(monkeypatch, capsys):
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    config = types.ModuleType("hermes_cli.config")
    config.load_config_readonly = lambda: {"dashboard": {"public_url": "https://hands.example.com"}}
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config)
    monkeypatch.setattr(cli.store, "mint_pairing_code", lambda name: "pair-code")
    ctx = Context()
    cli.register_cli(ctx, has_seed=True)
    parser = argparse.ArgumentParser()
    ctx.setup_fn(parser)
    args = parser.parse_args(["pair", "my laptop"])

    ctx.handler(args)

    assert capsys.readouterr().out == (
        "Pair code: pair-code\n"
        "Run on the device: hermes-hands pair https://hands.example.com --name 'my laptop' --code=pair-code\n"
    )


def test_pair_cli_refuses_without_a_relay_seed(monkeypatch, capsys):
    minted = []
    monkeypatch.setattr(cli.store, "mint_pairing_code", lambda name: minted.append(name) or "pair-code")
    ctx = Context()
    cli.register_cli(ctx, has_seed=False)
    parser = argparse.ArgumentParser()
    ctx.setup_fn(parser)

    ctx.handler(parser.parse_args(["pair", "laptop"]))

    assert capsys.readouterr().out == cli.messages.NOT_SET_UP + "\n"
    assert minted == []


def test_pair_cli_falls_back_to_dashboard_url(monkeypatch, capsys):
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    config = types.ModuleType("hermes_cli.config")
    config.load_config_readonly = lambda: {}
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config)
    monkeypatch.setattr(cli.store, "mint_pairing_code", lambda name: "pair-code")
    ctx = Context()
    ctx.config["dashboard_url"] = "http://127.0.0.1:9119"
    cli.register_cli(ctx, has_seed=True)
    parser = argparse.ArgumentParser()
    ctx.setup_fn(parser)

    ctx.handler(parser.parse_args(["pair", "laptop"]))

    assert "hermes-hands pair http://127.0.0.1:9119 --name laptop --code=pair-code" in capsys.readouterr().out

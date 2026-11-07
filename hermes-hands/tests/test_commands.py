import sys
import types
from enum import Enum

from hands import commands


class Platform(str, Enum):
    LOCAL = "local"
    TELEGRAM = "telegram"


class Source:
    def __init__(self, user_id="42", platform=Platform.TELEGRAM, chat_type="dm"):
        self.user_id = user_id
        self.platform = platform
        self.chat_type = chat_type


class Context:
    def __init__(self, owner_ids=None):
        self.owner_ids = owner_ids or []
        self.handler = None
        self.config = {}
        self.choice = None

    def get_config(self, key, default=None):
        return self.config.get(key, self.owner_ids if key == "owner_ids" else default)

    def register_command(self, name, handler, **kwargs):
        self.handler = handler

    def ask_choice(self, question, choices):
        return self.choice


def install_gateway(monkeypatch, extras=None):
    gateway = types.ModuleType("gateway")
    gateway.__path__ = []
    session = types.ModuleType("gateway.session")
    session.Platform = Platform
    config = types.ModuleType("gateway.config")
    config.load_gateway_config = lambda: types.SimpleNamespace(
        platforms={Platform.TELEGRAM: types.SimpleNamespace(extra=extras or {})}
    )
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.session", session)
    monkeypatch.setitem(sys.modules, "gateway.config", config)


def setup(monkeypatch, owner_ids=None):
    install_gateway(monkeypatch)
    ctx = Context(owner_ids)
    commands.register_commands(ctx)
    return ctx


def test_local_owner_authorized(monkeypatch):
    ctx = setup(monkeypatch)
    assert commands._is_owner(ctx, "desktop", Source(platform=Platform.LOCAL))


def test_owner_ids_authorized(monkeypatch):
    ctx = setup(monkeypatch, ["telegram:42"])
    assert commands._is_owner(ctx, "telegram", Source())


def test_platform_admin_allowlist_authorized(monkeypatch):
    install_gateway(monkeypatch, {"allow_admin_from": "7, 42"})
    assert commands._is_owner(Context(), "telegram", Source())


def test_empty_admin_list_and_falsy_user_refuse(monkeypatch):
    install_gateway(monkeypatch)
    ctx = Context()
    assert not commands._is_owner(ctx, "telegram", Source())
    assert not commands._is_owner(ctx, "telegram", Source(user_id=""))


def test_pair_reply_has_exact_device_command(monkeypatch):
    ctx = setup(monkeypatch, ["telegram:42"])
    ctx.config["dashboard_url"] = "https://gateway.example"
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    hermes_config = types.ModuleType("hermes_cli.config")
    hermes_config.load_config_readonly = lambda: {"dashboard": {}}
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", hermes_config)
    monkeypatch.setattr(commands.store, "mint_pairing_code", lambda name: "secret-code")
    result = ctx.handler("pair laptop", session_key="s", platform="telegram", source=Source())
    assert "hermes-hands pair https://gateway.example --name laptop --code secret-code" in result


def test_use_refuses_unknown_device(monkeypatch):
    ctx = setup(monkeypatch, ["telegram:42"])
    monkeypatch.setattr(commands.store, "get_device", lambda name: None)
    assert "No paired device" in ctx.handler("use missing", session_key="s", platform="telegram", source=Source())


def test_status_appends_device_policy_alerts(monkeypatch):
    ctx = setup(monkeypatch)
    monkeypatch.setattr(commands.store, "list_devices", lambda: [{"name": "laptop", "online": True, "policy_ok": False}])
    monkeypatch.setattr(commands.store, "relay_alive", lambda: True)
    monkeypatch.setattr(commands.messages, "alerts", lambda devices: ["Policy alert: laptop"])
    result = ctx.handler("status")
    assert result == "Hands: relay up; target server; 1 device(s).\nPolicy alert: laptop"


def test_revoke_requires_exact_yes(monkeypatch):
    ctx = setup(monkeypatch, ["telegram:42"])
    calls = []
    monkeypatch.setattr(commands.store, "revoke", lambda name: calls.append(name) or True)
    ctx.choice = "Yes"
    assert ctx.handler("revoke laptop", session_key="s", platform="telegram", source=Source()) == "Revoke cancelled."
    assert not calls
    ctx.choice = "yes"
    assert ctx.handler("revoke laptop", session_key="s", platform="telegram", source=Source()) == "Device revoked."
    assert calls == ["laptop"]

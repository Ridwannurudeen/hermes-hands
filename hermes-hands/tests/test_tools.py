import json
import importlib
import sys
import types

from hands import store


class Context:
    def __init__(self):
        self.handlers = {}
        self.cron = None

    def current_cron_execution(self):
        return self.cron

    def get_config(self, key, default=None):
        return default

    def register_tool(self, *, name, handler, **kwargs):
        self.handlers[name] = handler


def load_tools(monkeypatch, screenshot=None, session_key=""):
    client = types.ModuleType("hands.client")
    client.request_screenshot = screenshot or (lambda device: b"png")
    monkeypatch.setitem(sys.modules, "hands.client", client)
    import hands

    monkeypatch.setattr(hands, "client", client, raising=False)
    gateway = types.ModuleType("gateway")
    gateway.__path__ = []
    session_context = types.ModuleType("gateway.session_context")
    session_context.get_session_env = lambda key, default="": session_key
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.session_context", session_context)
    sys.modules.pop("hands.tools", None)
    tools = importlib.import_module("hands.tools")

    ctx = Context()
    tools.register_tools(ctx)
    return ctx


def test_screenshot_refuses_cron(monkeypatch):
    ctx = load_tools(monkeypatch, session_key="session")
    ctx.cron = object()
    result = json.loads(ctx.handlers["hands_screenshot"]({}))
    assert "cron" in result["error"]


def test_screenshot_refuses_server(monkeypatch):
    monkeypatch.setattr(store, "get_session_target", lambda key: None)
    ctx = load_tools(monkeypatch, session_key="session")
    result = json.loads(ctx.handlers["hands_screenshot"]({}))
    assert "device" in result["error"]


def test_screenshot_returns_media_line(monkeypatch, tmp_path):
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: tmp_path
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    monkeypatch.setattr("hands.targets.store.get_session_target", lambda key: "laptop")
    monkeypatch.setattr(store, "get_device", lambda name: {"name": name})
    ctx = load_tools(monkeypatch, session_key="session")
    result = json.loads(ctx.handlers["hands_screenshot"]({}))
    path = result["text"].split("MEDIA:", 1)[1]
    assert path.endswith(".png")
    assert (tmp_path / "cache" / "hermes-hands" / path.split("/")[-1]).read_bytes() == b"png"


def test_screenshot_errors_are_json(monkeypatch):
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: __import__("pathlib").Path(".")
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    monkeypatch.setattr(store, "get_session_target", lambda key: "laptop")
    monkeypatch.setattr(store, "get_device", lambda name: {"name": name})
    ctx = load_tools(monkeypatch, screenshot=lambda device: (_ for _ in ()).throw(RuntimeError("offline")), session_key="session")
    assert json.loads(ctx.handlers["hands_screenshot"]({})) == {"error": "offline"}

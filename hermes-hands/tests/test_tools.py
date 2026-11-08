import json
import importlib
import sys
import types
import os
import time

import pytest

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
    cache_dir = tmp_path / "cache" / "hermes-hands"
    cache_dir.mkdir(parents=True)
    old_png = cache_dir / "old.png"
    new_png = cache_dir / "new.png"
    unrelated = cache_dir / "old.jpg"
    outside = tmp_path / "cache" / "outside.png"
    for file in (old_png, new_png, unrelated, outside):
        file.write_bytes(b"old")
    old_time = time.time() - 24 * 60 * 60 - 1
    os.utime(old_png, (old_time, old_time))
    os.utime(unrelated, (old_time, old_time))
    os.utime(outside, (old_time, old_time))
    symlink = cache_dir / "linked.png"
    try:
        symlink.symlink_to(old_png)
    except OSError:
        symlink = None
    monkeypatch.setattr(
        "hands.targets.store.get_session_target", lambda key: ("laptop", "session:session")
    )
    monkeypatch.setattr(store, "get_device", lambda name: {"name": name})
    ctx = load_tools(monkeypatch, session_key="session")
    result = json.loads(ctx.handlers["hands_screenshot"]({}))
    path = result["text"].split("MEDIA:", 1)[1]
    assert path.endswith(".png")
    assert (tmp_path / "cache" / "hermes-hands" / path.split("/")[-1]).read_bytes() == b"png"
    assert not old_png.exists()
    assert new_png.exists()
    assert unrelated.exists()
    assert outside.exists()
    if symlink is not None:
        assert symlink.is_symlink()


def test_the_model_gets_each_tool_with_a_description_and_parameters(monkeypatch, tmp_path):
    """The definitions core sends the model (``registry.get_definitions``, then the schema sanitizer,
    model_tools.py:521) carry the description and parameters, and nothing else at the function level."""
    pytest.importorskip("tools.registry")
    from hermes_cli.plugins import PluginContext, PluginManager
    from hermes_cli.plugins_manifest import PluginManifest
    from tools.registry import registry
    from tools.schema_sanitizer import sanitize_tool_schemas

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    manager = PluginManager()
    names = {"hands_screenshot", "hands_devices"}
    importlib.import_module("hands.tools").register_tools(
        PluginContext(PluginManifest(name="hermes-hands", kind="backend"), manager)
    )
    try:
        definitions = sanitize_tool_schemas(registry.get_definitions(names))
    finally:
        for name in names:
            registry.deregister(name, scope=manager.scope_key)
    assert {definition["function"]["name"] for definition in definitions} == names
    for definition in definitions:
        function = definition["function"]
        assert set(function) == {"name", "description", "parameters"}
        assert function["description"]
        assert function["parameters"]["type"] == "object"
        assert function["parameters"]["properties"] == {}


def test_screenshot_errors_are_json(monkeypatch):
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: __import__("pathlib").Path(".")
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    monkeypatch.setattr(store, "get_session_target", lambda key: ("laptop", "session:session"))
    monkeypatch.setattr(store, "get_device", lambda name: {"name": name})
    ctx = load_tools(monkeypatch, screenshot=lambda device: (_ for _ in ()).throw(RuntimeError("offline")), session_key="session")
    assert json.loads(ctx.handlers["hands_screenshot"]({})) == {"error": "offline"}

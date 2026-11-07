"""Contract tests for ``register()`` and ``dashboard/plugin_api.py`` with real Hermes auth plumbing:
the plugin package is imported the way the plugin loader does (``hermes_plugins.<slug>``), the
dashboard file on its own, and requests go through the real token-auth middleware."""

import importlib.util
import sys
import time
import types
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("hermes_cli.dashboard_auth.token_auth")
pytest.importorskip("agent.secret_scope")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from hermes_cli.dashboard_auth import registry, token_auth  # noqa: E402

from hands import protocol  # noqa: E402
from hands_fakes import hands_db  # noqa: E402,F401  (fixture)

PLUGIN_DIR = Path(__file__).resolve().parents[1]
SEED = "9a" * 32


class Ctx:
    def __init__(self):
        self.config = {}
        self.calls = []

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_terminal_environment_provider(self, provider):
        self.calls.append(("terminal", provider))

    def register_dashboard_auth_provider(self, provider):
        registry.register_global_provider(provider)  # what PluginContext does (plugins.py:737-778)
        self.calls.append(("auth", provider))

    def register_command(self, name, handler, **kwargs):
        self.calls.append(("command", name))

    def register_cli_command(self, name, *args, **kwargs):
        self.calls.append(("cli", name))

    def register_tool(self, *, name, **kwargs):
        self.calls.append(("tool", name))


def load_plugin_package():
    """``hermes_plugins.hermes_hands`` exactly as plugins_loader.py:811-837 imports it."""
    sys.modules.setdefault("hermes_plugins", types.ModuleType("hermes_plugins")).__path__ = []
    name = "hermes_plugins.hermes_hands"
    for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
        del sys.modules[key]
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_plugin_api():
    """The dashboard file on its own, as web_server_dashboard.py:1233-1246 imports it."""
    name = "hermes_dashboard_plugin_hermes-hands"
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def plugin(hands_db, monkeypatch):
    monkeypatch.setenv("HANDS_RELAY_SEED", SEED)
    package = load_plugin_package()
    ctx = Ctx()
    package.register(ctx)
    hands = sys.modules["hermes_plugins.hermes_hands.hands"]
    monkeypatch.setattr(hands.relay.Relay, "start", lambda self: None)
    monkeypatch.setattr(hands.relay, "_TICK", 0.01)
    provider = dict((kind, obj) for kind, obj in ctx.calls if kind == "auth")["auth"]
    yield ctx, hands, provider
    registry.unregister_global_provider(provider.name, provider)
    token_auth.clear_token_routes()
    hands.client.configure("http://127.0.0.1:9119", None)


def make_app(plugin_api, *, gateway: bool = False) -> TestClient:
    app = FastAPI()
    app.middleware("http")(token_auth.token_auth_middleware)
    app.include_router(plugin_api.router, prefix="/api/plugins/hermes-hands")
    if gateway:
        app.state.gateway_runner = object()
    return TestClient(app)


def signed(device_id, private, path, body: bytes) -> dict:
    ts, nonce = int(time.time()), protocol.new_nonce()
    return {
        "Authorization": "Bearer " + protocol.make_bearer(device_id, private, ts=ts, nonce=nonce),
        "X-Hands-Signature": protocol.sign_request(
            private, protocol.D2R, ts, nonce, "POST", path, body
        ),
        "Content-Type": "application/json",
    }


def test_register_wires_everything(plugin):
    ctx, hands, provider = plugin
    kinds = [kind for kind, _ in ctx.calls]
    assert kinds.count("terminal") == 1 and kinds.count("auth") == 1
    assert ("command", "hands") in ctx.calls and ("cli", "hands") in ctx.calls
    assert {("tool", "hands_screenshot"), ("tool", "hands_devices")} <= set(ctx.calls)
    assert provider.keys == hands.relay.derive_keys(SEED)
    assert all(token_auth.is_token_route(path) for path in hands.relay.TOKEN_ROUTES)
    assert (
        str(hands.client._http.base_url).rstrip("/")
        == "http://127.0.0.1:9119/api/plugins/hermes-hands"
    )


def test_register_without_seed_still_serves_the_server_target(hands_db, monkeypatch):
    monkeypatch.delenv("HANDS_RELAY_SEED", raising=False)
    package = load_plugin_package()
    ctx = Ctx()
    package.register(ctx)
    provider = dict((kind, obj) for kind, obj in ctx.calls if kind == "auth")["auth"]
    try:
        assert provider.keys is None
        assert [kind for kind, _ in ctx.calls].count("terminal") == 1
        api = make_app(load_plugin_api())
        reply = api.post(
            "/api/plugins/hermes-hands/local/state", headers={"Authorization": "Bearer x"}, json={}
        )
        assert reply.status_code == 401  # nothing verifies without a seed
    finally:
        registry.unregister_global_provider(provider.name, provider)
        token_auth.clear_token_routes()


def test_device_and_local_routes_end_to_end(plugin):
    ctx, hands, provider = plugin
    api = make_app(load_plugin_api())
    base = "/api/plugins/hermes-hands"
    private, public = protocol.generate_keypair()
    code = hands.store.mint_pairing_code("laptop")
    fields = {
        "name": "laptop",
        "os": "Windows-11",
        "home": "C:\\Users\\r",
        "public_key": protocol.b64u(public),
        "policy_hash": "p0",
        "protocol": protocol.PROTOCOL_VERSION,
    }
    pair_body = protocol.canonical_json(
        {**fields, "sig": protocol.b64u(protocol.sign(private, protocol.canonical_json(fields)))}
    )
    paired = api.post(
        base + "/pair", content=pair_body, headers={"Authorization": f"Bearer {code}"}
    )
    assert paired.status_code == 200, paired.text
    assert protocol.verify_request(
        provider.keys.relay_public,
        paired.headers["X-Hands-Signature"],
        protocol.R2D,
        int(paired.headers["X-Hands-Ts"]),
        paired.headers["X-Hands-Nonce"],
        "POST",
        base + "/pair",
        paired.content,
    )
    device_id = paired.json()["device_id"]

    local = {"Authorization": f"Bearer {provider.keys.local_bearer}"}
    state = api.post(base + "/local/state", headers=local, json={})
    assert state.status_code == 200 and state.json()["devices"][0]["name"] == "laptop"
    forwarded = api.post(
        base + "/local/state", headers={**local, "X-Forwarded-For": "1.2.3.4"}, json={}
    )
    assert forwarded.status_code == 403

    body = protocol.canonical_json({"wait": 0})
    poll = api.post(
        base + "/device/poll",
        content=body,
        headers=signed(device_id, private, base + "/device/poll", body),
    )
    assert poll.status_code == 204
    replayed_headers = signed(device_id, private, base + "/device/poll", body)
    assert (
        api.post(base + "/device/poll", content=body, headers=replayed_headers).status_code == 204
    )
    replay = api.post(base + "/device/poll", content=body, headers=replayed_headers)
    assert (
        replay.status_code == 401 and replay.json()["error"] == "unauthenticated"
    )  # nonce burned by the seam
    device_on_local = api.post(
        base + "/local/state",
        headers=signed(device_id, private, base + "/local/state", b"{}"),
        content=b"{}",
    )
    assert device_on_local.status_code == 401

    hands.store.revoke("laptop")
    gone = api.post(
        base + "/device/poll",
        content=body,
        headers=signed(device_id, private, base + "/device/poll", body),
    )
    assert gone.status_code == 401 and gone.json()["error"] == "unknown_device"
    assert "X-Hands-Signature" in gone.headers

    ui = api.get(base + "/ui/state")
    assert ui.status_code == 200 and ui.json() == {"relay": True, "devices": []}


def test_gateway_copy_stays_inert(plugin):
    ctx, hands, provider = plugin
    api = make_app(load_plugin_api(), gateway=True)
    local = {"Authorization": f"Bearer {provider.keys.local_bearer}"}
    reply = api.post("/api/plugins/hermes-hands/local/state", headers=local, json={})
    assert reply.status_code == 503 and reply.json()["error"] == "relay_elsewhere"
    assert hands.relay._relay is None

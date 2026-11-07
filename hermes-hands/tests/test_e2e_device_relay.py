"""End to end in one process, no network: the real device agent (``device/hermeshands_device``:
``cli._pair``, ``DeviceLoop``, executor, policy, keys with the keyring disabled and a temp home)
against the real gateway half (``register()``, the token provider behind the real token-auth
middleware, ``dashboard/plugin_api.py``, the relay, the loopback client, ``HandsEnvironment`` and
core's own ``BaseEnvironment``/``ShellFileOperations``). An httpx MockTransport hands every request
from either half to a TestClient of the dashboard app; it can also answer a device poll with a forged
reply. This is the only test that imports from both halves.

Needs FastAPI and Hermes core importable (``hermes_cli``, ``tools``), and bash for the device agent
(Git Bash on Windows); skipped otherwise.
"""

import functools
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import psutil
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("hermes_cli.dashboard_auth.token_auth")
pytest.importorskip("tools.environments.base")
pytest.importorskip("tools.file_operations")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "device"))

from hermes_cli.dashboard_auth import registry, token_auth  # noqa: E402
from hermeshands_device import cli as device_cli  # noqa: E402
from hermeshands_device import config as device_config  # noqa: E402
from hermeshands_device import executor as device_executor  # noqa: E402
from hermeshands_device import keys as device_keys  # noqa: E402
from hermeshands_device import loop as device_loop  # noqa: E402
from hermeshands_device import policy as device_policy  # noqa: E402
from hermeshands_device import protocol as device_protocol  # noqa: E402
from tools.file_operations import ShellFileOperations  # noqa: E402
from tools.interrupt import set_interrupt  # noqa: E402

from hands_fakes import (  # noqa: E402
    RegisterCtx,
    hands_db,  # noqa: F401  (fixture)
    load_plugin_api,
    load_plugin_package,
    make_app,
)

pytestmark = pytest.mark.skipif(
    device_executor.resolve_bash() is None,
    reason="the device agent needs bash (Git Bash on Windows)",
)

SEED = "5e" * 32
GATEWAY = "https://hands.test"
API = "/api/plugins/hermes-hands"


def until(condition, *, what: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


class Agent:
    """``hermes-hands serve`` on a thread: the real DeviceLoop over the in-process transport."""

    def __init__(self, transport):
        self.loop = device_loop.DeviceLoop(transport=transport)
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        try:
            self.loop.run_forever()
        except BaseException as exc:  # recorded: the revoke test asserts how the agent ended
            self.error = exc

    def job_tree(self) -> list:
        """The processes of the device's running exec jobs: each bash and its children."""
        tree = []
        for proc, _started in list(self.loop.processes.values()):
            try:
                parent = psutil.Process(proc.pid)
                tree += [parent, *parent.children(recursive=True)]
            except psutil.NoSuchProcess:
                continue
        return tree

    def stop(self) -> None:
        self.loop.stop_event.set()
        self.thread.join(15)


def alive(processes) -> list:
    living = []
    for process in processes:
        try:
            if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                living.append(process)
        except psutil.NoSuchProcess:
            continue
    return living


@pytest.fixture
def rig(hands_db, tmp_path, monkeypatch):
    """A laptop paired by the real device code with the real gateway half, its agent running."""
    home = tmp_path / "laptop home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))  # Path.home() on Windows
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HANDS_RELAY_SEED", SEED)
    monkeypatch.setenv("HERMES_SESSION_KEY", "s1")
    root = home / ".hermes-hands"
    for attr, path in (
        ("ROOT", root),
        ("CONFIG_PATH", root / "config.json"),
        ("POLICY_PATH", root / "policy.json"),
        ("KEY_PATH", root / "device.json"),
        ("OUT_DIR", root / "out"),
    ):
        monkeypatch.setattr(device_config, attr, path)
    monkeypatch.setattr(device_keys, "_keyring", lambda: None)

    package = load_plugin_package()
    api = make_app(load_plugin_api(), base_url=GATEWAY)
    forgeries: list = []  # each answers one device poll instead of the dashboard
    delivered: list = []  # every job the relay handed the device

    def dashboard(request: httpx.Request) -> httpx.Response:
        if request.url.path == API + "/device/poll" and forgeries:
            return forgeries.pop(0)(request)
        headers = {
            k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")
        }
        reply = api.request(
            request.method, request.url.path, content=request.content, headers=headers
        )
        if request.url.path == API + "/device/poll" and reply.status_code == 200:
            delivered.append(json.loads(reply.content))
        return httpx.Response(reply.status_code, headers=reply.headers, content=reply.content)

    transport = httpx.MockTransport(dashboard)
    ctx = RegisterCtx()
    with monkeypatch.context() as scoped:  # register() builds the gateway's loopback client
        scoped.setattr(httpx, "Client", functools.partial(httpx.Client, transport=transport))
        package.register(ctx)
    provider = dict(ctx.calls)["auth"]
    hands = sys.modules["hermes_plugins.hermes_hands.hands"]  # imported by register()
    monkeypatch.setattr(hands.relay.Relay, "start", lambda self: None)  # no heartbeat thread
    monkeypatch.setattr(hands.relay, "MAX_POLL_WAIT", 1)  # the agent's 25 s long-poll, shortened

    code = hands.store.mint_pairing_code("laptop")
    pairing = httpx.Client(transport=transport)
    with monkeypatch.context() as scoped:  # cli._pair posts with httpx.post
        scoped.setattr(httpx, "post", lambda url, **kwargs: pairing.post(url, **kwargs))
        assert device_cli._pair(GATEWAY, "laptop", code) == 0
    hands.store.set_session_target("s1", "laptop")
    agent = Agent(transport)
    environments = []

    def environment():
        env = hands.environment.HandsEnvironment(ctx, cwd="~", timeout=30)
        environments.append(env)
        return env

    yield SimpleNamespace(
        hands=hands,
        home=home,
        device_home=hands.environment.device_path(str(home)),
        agent=agent,
        keys=provider.keys,
        forgeries=forgeries,
        delivered=delivered,
        environment=environment,
    )
    agent.stop()
    for env in environments:
        env.cleanup()
    registry.unregister_global_provider(provider.name, provider)
    token_auth.clear_token_routes()
    hands.client.configure("http://127.0.0.1:9119", None)


def test_pairing_then_terminal_and_file_writes(rig):
    device = rig.hands.store.get_device("laptop")
    assert device["home"] == str(rig.home) and device["online"] and device["policy_ok"]
    saved = json.loads(device_config.CONFIG_PATH.read_text(encoding="utf-8-sig"))
    assert saved["device_id"] == device["device_id"]
    assert device_protocol.unb64u(saved["relay_public_key"]) == rig.keys.relay_public
    assert device_config.KEY_PATH.exists()  # keyring disabled: the key file

    env = rig.environment()
    result = env.execute("echo hello from the laptop; pwd", bounded_capture=True)
    assert result["returncode"] == 0, result
    note, said, where = result["output"].splitlines()[:3]
    assert note.startswith("hands: now on laptop (") and said == "hello from the laptop"
    assert where == rig.device_home

    files = ShellFileOperations(env)
    big = "".join(
        f"line {i:06d} of a write far past the Windows command-line limit\n" for i in range(4000)
    )
    assert len(big.encode("utf-8")) >= 200_000
    for name, content in (
        ("no-newline.txt", "one\ntwo"),
        ("newline.txt", "one\ntwo\n"),
        ("big.txt", big),
    ):
        written = files.write_file(f"{rig.device_home}/{name}", content)
        assert written.error is None and written.verified is True, written.to_dict()
        assert (rig.home / name).read_bytes() == content.encode("utf-8")


def test_timeout_and_cancel_stop_the_work_on_the_device(rig):
    env = rig.environment()
    assert env.execute("true")["returncode"] == 0

    timed_out = {}
    worker = threading.Thread(target=lambda: timed_out.update(env.execute("sleep 20", timeout=3)))
    worker.start()
    until(lambda: len(rig.agent.job_tree()) > 1, what="the device to start sleep")
    tree = rig.agent.job_tree()
    worker.join(20)
    assert timed_out["returncode"] == 124 and timed_out.get("hermes_timed_out") is True
    until(lambda: not alive(tree), what="the device to kill the timed-out job's process tree")

    cancelled = {}
    worker = threading.Thread(target=lambda: cancelled.update(env.execute("sleep 20", timeout=30)))
    worker.start()
    until(lambda: len(rig.agent.job_tree()) > 1, what="the device to start sleep")
    tree = rig.agent.job_tree()
    set_interrupt(True, thread_id=worker.ident)  # what /stop does to the tool thread
    try:
        worker.join(20)
    finally:
        set_interrupt(False, thread_id=worker.ident)
    assert cancelled["returncode"] == 130 and cancelled.get("hermes_interrupted") is True
    until(lambda: not alive(tree), what="the device to kill the cancelled job's process tree")
    assert env.execute("echo still serving")["output"].strip() == "still serving"


def test_forged_relay_replies_are_dropped(rig, caplog):
    caplog.set_level("WARNING", logger=device_loop.logger.name)
    env = rig.environment()
    assert env.execute("true")["returncode"] == 0
    evil = device_protocol.canonical_json(
        {
            "job_id": "forged",
            "type": "exec",
            "script": "echo pwned > pwned.txt",
            "login": False,
            "timeout": 5,
        }
    )
    attacker, _ = device_protocol.generate_keypair()

    def reply(private, signed_body, *, nonce=None):
        def forged(request):
            ts, used = int(time.time()), nonce or request.headers["X-Hands-Nonce"]
            signature = device_protocol.sign_request(
                private, device_protocol.R2D, ts, used, "POST", request.url.path, signed_body
            )
            headers = {"X-Hands-Ts": str(ts), "X-Hands-Nonce": used, "X-Hands-Signature": signature}
            return httpx.Response(200, content=evil, headers=headers)

        return forged

    rig.forgeries += [
        reply(attacker, evil),  # signed by a key the device never pinned
        reply(
            rig.keys.relay_private, b'{"job_id":"x","type":"cancel","target_job_id":"y"}'
        ),  # body swapped
        reply(
            rig.keys.relay_private, evil, nonce=device_protocol.new_nonce()
        ),  # genuine, for another request
    ]
    until(lambda: not rig.forgeries, what="the device to receive the forged replies")
    until(
        lambda: caplog.text.count("Dropping invalid relay reply") >= 3,
        what="the device to drop them",
    )
    assert env.execute("echo still serving")["output"].strip() == "still serving"
    assert not (rig.home / "pwned.txt").exists()


def test_policy_change_is_refused_until_accepted(rig):
    env = rig.environment()
    assert env.execute("echo before")["returncode"] == 0
    edited = device_protocol.canonical_json(
        {"deny": [*device_policy.DEFAULTS, r"\bnever-matches\b"]}
    )
    device_config.POLICY_PATH.write_bytes(edited)
    refused = env.execute("echo after the edit", timeout=15)
    assert refused["returncode"] == 255
    assert rig.hands.messages.policy_changed("laptop") in refused["output"]
    assert rig.hands.store.get_device("laptop")["policy_ok"] is False
    again = env.execute("echo still refused", timeout=15)
    assert (
        again["returncode"] == 255
        and rig.hands.messages.policy_changed("laptop") in again["output"]
    )
    # The agent must keep polling while refused, or accept-policy cannot bring it back.
    assert not rig.agent.loop.stop_event.is_set(), (
        "the device agent stopped itself on policy_changed"
    )
    assert rig.hands.store.accept_policy("laptop")
    accepted = env.execute("echo accepted", timeout=15)
    assert accepted["returncode"] == 0 and accepted["output"].strip() == "accepted", accepted


def test_revoke_mid_command_stops_the_device(rig):
    env = rig.environment()
    assert env.execute("true")["returncode"] == 0
    outcome = {}
    worker = threading.Thread(target=lambda: outcome.update(env.execute("sleep 20", timeout=30)))
    worker.start()
    until(lambda: len(rig.agent.job_tree()) > 1, what="the device to start sleep")
    tree = rig.agent.job_tree()
    assert rig.hands.store.revoke("laptop")
    worker.join(20)
    assert (
        outcome["returncode"] == 255 and rig.hands.messages.revoked("laptop") in outcome["output"]
    )
    rig.agent.thread.join(20)
    assert not rig.agent.thread.is_alive()
    assert isinstance(rig.agent.error, PermissionError) and str(rig.agent.error) == "unknown_device"
    until(lambda: not alive(tree), what="the device to kill its running work")


def test_background_job_follows_the_device_across_a_switch(rig):
    """U22: core's process registry launches, polls and kills through the environment it launched
    with (process_registry.py:1271-1300, 1418-1466, 2245-2246); after /hands use server those calls
    must still reach the device."""
    from tools.process_registry import process_registry

    env = rig.environment()
    assert env.execute("true")["returncode"] == 0
    started = time.monotonic()
    session = process_registry.spawn_via_env(
        env, "echo started; sleep 45", task_id="e2e-bg", timeout=10
    )
    assert session.pid is not None
    assert time.monotonic() - started < 8, (
        "the launch waited for the background job instead of returning"
    )
    rig.hands.store.set_session_target("s1", "server")  # /hands use server
    until(
        lambda: "started" in process_registry.poll(session.id)["output_preview"],
        what="the registry to read the job's log on the device",
    )
    time.sleep(4)  # past core's 10 s launch timeout: the job must still be running on the device
    assert not process_registry.get(session.id).exited, process_registry.poll(session.id)
    seen = len(rig.delivered)
    process_registry.kill_process(session.id)
    until(
        lambda: any(
            f"kill {session.pid} 2>/dev/null" in job.get("script", "")
            for job in rig.delivered[seen:]
        ),
        what="the kill to reach the device",
    )

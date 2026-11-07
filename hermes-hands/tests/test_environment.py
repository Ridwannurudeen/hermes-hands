"""Contract tests against real Hermes core (``BaseEnvironment``, ``LocalEnvironment``, the session
ContextVar): the device side is a fake agent that runs each job with a local bash, through a real
Relay and the loopback client. Skipped when Hermes is not importable."""

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("tools.environments.base")
pytest.importorskip("gateway.session_context")
pytest.importorskip("agent.terminal_env_provider")

from hands import messages, relay, store  # noqa: E402
from hands.environment import DeviceEnvironment, HandsEnvironment, device_path  # noqa: E402
from hands.provider import HandsProvider  # noqa: E402
from hands_fakes import Agent, Device, hands_db, wired  # noqa: E402,F401  (fixtures)

GIT_BASH = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"
BASH = str(GIT_BASH) if os.name == "nt" else shutil.which("bash")
pytestmark = pytest.mark.skipif(not BASH or not Path(BASH).exists(), reason="needs bash")

release = threading.Event()


def bash_job(job):
    """What the device agent does with an exec job, marked so device output is recognisable."""
    if "sleep 99" in job["script"]:
        release.wait(10)
        return "", 0
    args = [BASH, *(["-l"] if job["login"] else []), "-c", job["script"]]
    env = {**os.environ, "HANDS_FAKE_DEVICE": "device"}
    done = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
        timeout=60,
    )
    return done.stdout.decode("utf-8", "replace"), done.returncode


class Ctx:
    def __init__(self):
        self.cron = None
        self.config = {}

    def current_cron_execution(self):
        return self.cron

    def get_config(self, key, default=None):
        return self.config.get(key, default)


@pytest.fixture
def laptop(wired, tmp_path, monkeypatch):
    """A paired 'laptop' whose home is a temp dir, chosen by session s1; yields ``(hub, device, home)``."""
    hub, _ = wired
    home = tmp_path / "dev home"
    home.mkdir()
    server = tmp_path / "srv"
    server.mkdir()
    monkeypatch.setenv("MESSAGING_CWD", str(server))
    monkeypatch.setenv("HERMES_SESSION_KEY", "s1")
    device = Device(home=str(home), device_os="Windows-11" if os.name == "nt" else "Linux")
    store.set_session_target("s1", "laptop")
    release.clear()
    yield hub, device, home
    release.set()


def test_provider_flags():
    provider = HandsProvider(Ctx())
    assert (provider.name, provider.display_name) == ("hands", "Hermes Hands")
    assert provider.is_remote and provider.is_container and not provider.supports_streams
    assert provider.skip_container_guards is False
    assert provider.default_cwd == "~"
    assert provider.cache_path_base is None and provider.strip_env_keys == frozenset()
    assert provider.is_available() and provider.check_requirements({})


def test_device_path():
    assert device_path("C:\\Users\\me\\a b") == "/c/Users/me/a b"
    assert device_path("d:/x") == "/d/x"
    assert device_path("/home/me") == "/home/me"


def test_terminal_and_cd_on_the_device(laptop):
    hub, device, home = laptop
    with Agent(hub, device, bash_job) as agent:
        env = HandsEnvironment(Ctx(), cwd="~", timeout=30)
        assert env.cwd == device_path(str(home))  # absolute before core stamps the sandbox home
        first = env.execute("echo $HANDS_FAKE_DEVICE", bounded_capture=True)
        assert first["returncode"] == 0
        note, output = first["output"].split("\n", 1)
        assert note.startswith("hands: now on laptop (") and output.strip() == "device"
        assert agent.jobs[0]["login"] is True  # init_session's bootstrap ran on first use
        assert env._subs["laptop"]._snapshot_ready  # the login snapshot works under this bash
        assert env.execute("mkdir -p sub && cd sub")["returncode"] == 0
        assert env.execute("pwd")["output"].strip().endswith("/sub")
        assert "hands:" not in env.execute("echo again", bounded_capture=True)["output"]


def test_heredoc_write_is_byte_exact(laptop):
    hub, device, home = laptop
    with Agent(hub, device, bash_job) as agent:
        env = HandsEnvironment(Ctx(), cwd="~", timeout=30)
        for name, content in (
            ("a.txt", "one\ntwo"),
            ("b.txt", "one\ntwo\n"),
            ("c.txt", "naïve ✓\n"),
        ):
            result = env.execute(f"cat > '{name}'", stdin_data=content)
            assert result["returncode"] == 0, result
            assert (home / name).read_bytes() == content.encode("utf-8")
        assert any("HERMES_STDIN_" in job["script"] for job in agent.jobs)


def test_offline_at_first_use_then_recovery(laptop, monkeypatch):
    hub, device, home = laptop
    monkeypatch.setattr(store, "OFFLINE_AFTER", -1)
    env = HandsEnvironment(Ctx(), cwd="~", timeout=30)
    started = time.monotonic()
    result = env.execute("echo hi", bounded_capture=True)
    assert time.monotonic() - started < 5
    assert (
        result["returncode"] == relay.UNREACHABLE and messages.offline("laptop") in result["output"]
    )
    monkeypatch.setattr(store, "OFFLINE_AFTER", 20)
    with Agent(hub, device, bash_job) as agent:
        assert env.execute("echo $HANDS_FAKE_DEVICE")["output"].strip() == "device"
        assert agent.jobs[0]["login"] is True  # the bootstrap is retried after an offline init


def test_offline_after_success_keeps_cwd(laptop, monkeypatch):
    hub, device, home = laptop
    env = HandsEnvironment(Ctx(), cwd="~", timeout=30)
    with Agent(hub, device, bash_job):
        env.execute("mkdir -p kept && cd kept")
    monkeypatch.setattr(store, "OFFLINE_AFTER", -1)
    offline = env.execute("pwd")
    assert offline["returncode"] == relay.UNREACHABLE and "offline" in offline["output"]
    monkeypatch.setattr(store, "OFFLINE_AFTER", 20)
    with Agent(hub, device, bash_job):
        assert env.execute("pwd")["output"].strip().endswith("/kept")


def test_timeout_is_cores_124_and_cancels_the_job(laptop):
    hub, device, home = laptop
    with Agent(hub, device, bash_job) as agent:
        env = HandsEnvironment(Ctx(), cwd="~", timeout=30)
        env.execute("true")
        result = env.execute("sleep 99", timeout=1)
        assert result["returncode"] == 124 and result.get("hermes_timed_out") is True
        deadline = time.monotonic() + 5
        while not any(job["type"] == "cancel" for job in agent.jobs):
            release.set()  # let the fake agent finish so it can poll the cancel
            assert time.monotonic() < deadline, "no cancel reached the device"
            time.sleep(0.02)


def test_switch_to_server_keeps_each_targets_cwd(laptop, tmp_path):
    hub, device, home = laptop
    with Agent(hub, device, bash_job):
        env = HandsEnvironment(Ctx(), cwd="~", timeout=30)
        moved = env.execute("mkdir -p deep && cd deep")
        device_cwd = moved["cwd"]
        store.set_session_target("s1", "server")
        on_server = env.execute(
            "echo ${HANDS_FAKE_DEVICE:-server}; pwd", cwd=device_cwd, bounded_capture=True
        )
        lines = on_server["output"].splitlines()
        assert lines[0] == messages.TARGET_NOTE_SERVER and lines[1] == "server"
        assert lines[2].rstrip("/").endswith("srv")
        assert env.is_local is True and env.uses_host_filesystem is True
        store.set_session_target("s1", "laptop")
        back = env.execute("pwd", cwd=on_server.get("cwd", ""))
        assert back["output"].strip().endswith("/deep")
        assert env.is_local is False and env.uses_host_filesystem is False
        env.cleanup()


def test_switch_across_new_objects_ignores_the_other_targets_cwd(laptop):
    """A session outlives its backend object (idle reaper, gateway restart): the next object must not
    reuse the other target's recorded cwd, and notes its target on its first terminal command."""
    hub, device, home = laptop
    with Agent(hub, device, bash_job):
        first = HandsEnvironment(Ctx(), cwd="~", timeout=30)
        device_cwd = first.execute("mkdir -p deep && cd deep", bounded_capture=True)["cwd"]
        first.cleanup()
        store.set_session_target("s1", "server")
        second = HandsEnvironment(Ctx(), cwd="~", timeout=30)
        on_server = second.execute("pwd", cwd=device_cwd, bounded_capture=True)
        note, where = on_server["output"].splitlines()[:2]
        assert note == messages.TARGET_NOTE_SERVER and where.rstrip("/").endswith("srv")
        assert "hands:" not in second.execute("pwd", bounded_capture=True)["output"]
        second.cleanup()
        third = HandsEnvironment(Ctx(), cwd="~", timeout=30)
        assert third.execute("pwd", bounded_capture=True)["output"].startswith(messages.TARGET_NOTE_SERVER)
        third.cleanup()


def test_backend_survives_core_per_turn_teardown(laptop):
    from tools import terminal_tool
    from tools.terminal_tool_lifecycle import is_persistent_env

    env = HandsEnvironment(Ctx(), cwd="~", timeout=30)
    terminal_tool._active_environments["hands-test"] = env
    try:
        assert HandsEnvironment._persistent is True and is_persistent_env("hands-test") is True
    finally:
        terminal_tool._active_environments.pop("hands-test", None)


def test_sandbox_home_follows_the_target(laptop):
    from tools.terminal_tool_config import plugin_sandbox_home

    hub, device, home = laptop
    env = HandsEnvironment(Ctx(), cwd="~", timeout=30)
    env._hermes_sandbox_home = "/stamped/by/core/once"  # what terminal_tool_backends.py:231 does
    assert env._hermes_sandbox_home == device_path(str(home))
    if os.name != "nt":
        assert plugin_sandbox_home(env) == str(home)
    store.set_session_target("s1", "server")
    assert env._hermes_sandbox_home == env.cwd
    assert env._hermes_sandbox_home.rstrip("/\\").endswith("srv")
    store.set_session_target("s1", "ghost")
    assert env._hermes_sandbox_home is None and plugin_sandbox_home(env) is None


def test_store_failure_never_raises_into_core_and_runs_nothing(laptop, monkeypatch, caplog):
    import sqlite3

    hub, device, home = laptop

    def broken(session_key):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "get_session_target", broken)
    caplog.set_level("WARNING", logger="hands.environment")
    env = HandsEnvironment(Ctx(), cwd="~", timeout=30)  # create_environment must not raise
    server_cwd = env.cwd
    assert server_cwd.rstrip("/\\").endswith("srv") and env.is_local is True
    assert env.uses_host_filesystem is True and isinstance(env.env, dict)
    assert caplog.text.count("device store is unreadable") == 1  # logged once
    marker = Path(server_cwd) / "ran"
    result = env.execute("echo x > ran", bounded_capture=True)
    assert result == {"output": messages.STORE_DOWN, "returncode": relay.UNREACHABLE}
    assert not marker.exists()
    with pytest.raises(sqlite3.Error):
        env.fetch_realpath("/etc/hostname")  # a media fetch catches it (gateway/media_fetch.py:112-114)
    assert caplog.text.count("device store is unreadable") == 1


def test_cron_runs_on_the_server(laptop):
    ctx = Ctx()
    ctx.cron = object()
    env = HandsEnvironment(ctx, cwd="~", timeout=30)
    assert env.execute("echo ${HANDS_FAKE_DEVICE:-server}")["output"].strip() == "server"


def test_unknown_device_fails_closed(wired, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_KEY", "s2")
    store.set_session_target("s2", "ghost")
    env = HandsEnvironment(Ctx(), cwd="~", timeout=30)
    assert env.cwd == "~"
    result = env.execute("echo hi")
    assert result["returncode"] == relay.UNREACHABLE
    assert messages.unknown_device("ghost") in result["output"]


def test_device_run_bash_refuses_piped_stdin(wired):
    env = DeviceEnvironment("laptop", cwd="~", timeout=5, home=None)
    with pytest.raises(AssertionError):
        env._run_bash("cat", stdin_data="x")

import json
import types

from hermeshands_device import cli, config
from hermeshands_device.protocol import b64u


def test_config_round_trip_and_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    config.save({"gateway_url": "https://example.test/api/plugins/hermes-hands", "device_id": "device-1", "relay_public_key": b64u(b"k" * 32)})
    assert config.gateway_url() == "https://example.test/api/plugins/hermes-hands"
    assert config.device_id() == "device-1"
    assert config.relay_public_key() == b"k" * 32
    assert json.loads(config.CONFIG_PATH.read_text(encoding="utf-8-sig"))["device_id"] == "device-1"


def test_status_and_policy_cli(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(config, "POLICY_PATH", tmp_path / "policy.json")
    config.save({"gateway_url": "https://gateway.test/api/plugins/hermes-hands", "device_id": "device-1"})
    assert cli.main(["status"]) == 0
    assert "device-1" in capsys.readouterr().out
    assert cli.main(["policy", "reset"]) == 0
    assert cli.main(["policy", "show"]) == 0
    assert "sudo" in capsys.readouterr().out


def test_pair_accepts_base_and_plugin_prefix_urls(monkeypatch, capsys):
    requests = []
    monkeypatch.setattr(cli, "get_or_create", lambda: (b"p" * 32, b"q" * 32))
    monkeypatch.setattr(cli.policy, "digest", lambda: "policy")
    monkeypatch.setattr(
        cli.httpx,
        "post",
        lambda url, **kwargs: requests.append(url)
        or types.SimpleNamespace(status_code=401, text='{"detail":"Unauthorized"}'),
    )

    for url in (
        "https://hands.example.com/",
        "https://hands.example.com/api/plugins/hermes-hands/",
    ):
        assert cli._pair(url, "laptop", "code") == 1

    assert requests == [
        "https://hands.example.com/api/plugins/hermes-hands/pair",
        "https://hands.example.com/api/plugins/hermes-hands/pair",
    ]
    assert "unknown, expired or already used" in capsys.readouterr().err


def test_pair_refuses_insecure_non_loopback_url(monkeypatch, capsys):
    monkeypatch.setattr(cli, "get_or_create", lambda: (_ for _ in ()).throw(AssertionError("must validate first")))
    assert cli._pair("http://hands.example.com", "laptop", "code") == 1
    assert "https" in capsys.readouterr().err
    for url in ("http://localhost:9119", "http://127.0.0.1:9119", "http://[::1]:9119"):
        monkeypatch.setattr(cli, "get_or_create", lambda: (b"p" * 32, b"q" * 32))
        monkeypatch.setattr(cli.httpx, "post", lambda *args, **kwargs: types.SimpleNamespace(status_code=500, text="error"))
        assert cli._pair(url, "laptop", "code") == 1


def test_pair_reports_httpx_transport_error(monkeypatch, capsys):
    monkeypatch.setattr(cli, "get_or_create", lambda: (b"p" * 32, b"q" * 32))
    monkeypatch.setattr(cli.policy, "digest", lambda: "policy")
    def fail(url, **kwargs):
        raise cli.httpx.ConnectError("connection refused", request=cli.httpx.Request("POST", url))
    monkeypatch.setattr(cli.httpx, "post", fail)
    assert cli._pair("https://hands.example.com", "laptop", "code") == 1
    error = capsys.readouterr().err
    assert "https://hands.example.com/api/plugins/hermes-hands/pair" in error
    assert "connection refused" in error


def test_doctor_executes_python_probe_and_names_windowsapps(monkeypatch, capsys):
    monkeypatch.setattr(cli.executor, "resolve_bash", lambda: "bash.exe")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "git.exe")
    monkeypatch.setattr(cli.config, "gateway_url", lambda: "")
    monkeypatch.setattr(cli.sys, "platform", "win32")
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if "command -v python3" in args[-1]:
            return types.SimpleNamespace(returncode=0, stdout="/c/Users/u/AppData/Local/Microsoft/WindowsApps/python3\n")
        return types.SimpleNamespace(returncode=49, stdout="Python was not found\n")
    monkeypatch.setattr(cli.subprocess, "run", run)
    assert cli._doctor() == 1
    output = capsys.readouterr().out
    assert "WindowsApps alias" in output
    assert any('python3 -c "import sys; print(sys.version)"' in call[-1] for call in calls)


def test_doctor_requires_working_python3_when_python_works(monkeypatch, capsys):
    monkeypatch.setattr(cli.executor, "resolve_bash", lambda: "bash.exe")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "git.exe")
    monkeypatch.setattr(cli.config, "gateway_url", lambda: "")
    monkeypatch.setattr(cli.sys, "platform", "linux")

    def run(args, **kwargs):
        command = args[-1]
        if "command -v python3" in command:
            return types.SimpleNamespace(returncode=0, stdout="/usr/bin/python3\n")
        if "python3 -c" in command:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="python3 stub")
        if "command -v python" in command:
            return types.SimpleNamespace(returncode=0, stdout="/usr/bin/python\n")
        return types.SimpleNamespace(returncode=0, stdout="3.14\n")

    monkeypatch.setattr(cli.subprocess, "run", run)
    assert cli._doctor() == 1
    assert "/usr/bin/python" in capsys.readouterr().out


def test_doctor_reports_python_probe_timeout(monkeypatch, capsys):
    monkeypatch.setattr(cli.executor, "resolve_bash", lambda: "bash.exe")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "git.exe")
    monkeypatch.setattr(cli.config, "gateway_url", lambda: "")

    def run(args, **kwargs):
        if "python3 -c" in args[-1]:
            raise cli.subprocess.TimeoutExpired(args, kwargs["timeout"])
        if "command -v python3" in args[-1]:
            return types.SimpleNamespace(returncode=0, stdout="/usr/bin/python3\n")
        return types.SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(cli.subprocess, "run", run)
    assert cli._doctor() == 1
    assert "timed out while starting" in capsys.readouterr().out

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
    assert "use the base URL, e.g. https://hands.example.com" in capsys.readouterr().err

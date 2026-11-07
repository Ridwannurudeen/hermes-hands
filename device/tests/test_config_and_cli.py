import json

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

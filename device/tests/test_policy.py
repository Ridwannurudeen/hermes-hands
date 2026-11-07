from hermeshands_device import policy


def test_policy_refusal_names_pattern_and_126(capsys):
    result = policy.check("sudo rm -rf /")
    output = capsys.readouterr().out
    assert result == 126
    assert "sudo" in output


def test_policy_digest_and_reset(tmp_path, monkeypatch):
    monkeypatch.setattr("hermeshands_device.config.ROOT", tmp_path)
    monkeypatch.setattr("hermeshands_device.config.POLICY_PATH", tmp_path / "policy.json")
    first = policy.reset()
    assert first == policy.digest()
    assert policy.refusal("shutdown now")


def test_policy_blocks_terminating_its_own_pid():
    import os

    assert policy.refusal(f"taskkill /PID {os.getpid()}")


def test_policy_blocks_drive_root_removal():
    assert policy.refusal("Remove-Item C:\\ -Recurse -Force")

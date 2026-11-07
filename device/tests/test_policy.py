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


def test_policy_allows_repo_paths_and_server_pidfile():
    assert policy.refusal("builtin cd -- '/c/Users/me/hermes-hands' || exit 126") is None
    assert policy.refusal("kill -9 $(cat server.pid)") is None
    assert policy.refusal('kill -0 "$(cat /tmp/hermes_bg_proc-1.pid 2>/dev/null)" 2>/dev/null; echo $?') is None


def test_policy_blocks_sensitive_commands_after_quote_normalization(monkeypatch):
    monkeypatch.setattr(policy.os, "getpid", lambda: 4321)
    for script in (
        'hermes-hands unpair',
        'hermes""-hands unpair',
        'pipx uninstall hermes-hands',
        'rm -rf ~/.hermes-hands',
        'cat ~/.hermes-hands/config.json',
        'kill $PPID',
        'kill -9 4321',
        'Hermes"Hands"',
    ):
        assert policy.refusal(script), script

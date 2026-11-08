import os
import types
from pathlib import Path

import pytest

from hermeshands_device import config, keys, screen


def test_file_key_fallback_is_private_on_posix(monkeypatch, tmp_path):
    monkeypatch.setattr(keys, "_keyring", lambda: None)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "KEY_PATH", tmp_path / "device.json")
    private, public = keys.get_or_create()
    assert len(private) == len(public) == 32
    assert keys.get_or_create() == (private, public)
    if os.name != "nt":
        assert config.KEY_PATH.stat().st_mode & 0o777 == 0o600


def test_keyring_backend_error_falls_back_to_file(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "KEY_PATH", tmp_path / "device.json")

    class KeyringError(Exception):
        pass

    class BrokenKeyring:
        errors = types.SimpleNamespace(KeyringError=KeyringError)

        @staticmethod
        def get_password(service, username):
            raise KeyringError("no backend")

        @staticmethod
        def set_password(service, username, password):
            raise KeyringError("no backend")

    monkeypatch.setattr(keys, "_keyring", lambda: BrokenKeyring)
    pair = keys.get_or_create()
    assert keys.get_or_create() == pair
    assert config.KEY_PATH.exists()
    assert "using protected device key file" in caplog.text


def test_existing_file_key_is_authoritative_when_keyring_appears(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "KEY_PATH", tmp_path / "device.json")
    monkeypatch.setattr(keys, "_keyring", lambda: None)
    original = keys.get_or_create()

    class WorkingKeyring:
        errors = types.SimpleNamespace(KeyringError=Exception)

        @staticmethod
        def get_password(service, username):
            raise AssertionError("file key should be checked first")

    monkeypatch.setattr(keys, "_keyring", lambda: WorkingKeyring)
    assert keys.get_or_create() == original


def test_working_keyring_remains_in_use(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "KEY_PATH", tmp_path / "device.json")
    stored = {}

    class WorkingKeyring:
        errors = types.SimpleNamespace(KeyringError=Exception)

        @staticmethod
        def get_password(service, username):
            return stored.get((service, username))

        @staticmethod
        def set_password(service, username, password):
            stored[(service, username)] = password

    monkeypatch.setattr(keys, "_keyring", lambda: WorkingKeyring)
    pair = keys.get_or_create()
    assert keys.get_or_create() == pair
    assert not config.KEY_PATH.exists()
    assert stored


def test_keyring_set_error_falls_back_to_file(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "KEY_PATH", tmp_path / "device.json")

    class KeyringError(Exception):
        pass

    class WriteFailingKeyring:
        errors = types.SimpleNamespace(KeyringError=KeyringError)

        @staticmethod
        def get_password(service, username):
            return None

        @staticmethod
        def set_password(service, username, password):
            raise KeyringError("read only backend")

    monkeypatch.setattr(keys, "_keyring", lambda: WriteFailingKeyring)
    pair = keys.get_or_create()
    assert config.KEY_PATH.exists()
    assert keys.get_or_create() == pair


def test_screen_requires_explicit_flag():
    with pytest.raises(PermissionError, match="serve --allow-screen"):
        screen.capture(allow_screen=False)


def test_screen_capture_uses_pillow_png_methods(monkeypatch):
    shot = types.SimpleNamespace(size=(2, 2), rgb=bytes(12))

    class Capture:
        monitors = [None, object()]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def grab(self, monitor):
            assert monitor is self.monitors[1]
            return shot

    monkeypatch.setattr(screen.mss, "mss", Capture)
    result = screen.capture(allow_screen=True)
    assert result.startswith(b"\x89PNG\r\n\x1a\n")


def test_windows_keyfile_uses_sid_acl_and_atomic_replace(monkeypatch, tmp_path):
    monkeypatch.setattr(keys, "_keyring", lambda: None)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "KEY_PATH", tmp_path / "device.json")
    monkeypatch.setattr(keys, "os", types.SimpleNamespace(name="nt", replace=os.replace, environ={"SystemRoot": "C:\\Windows"}, path=os.path))
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[0].lower().endswith("whoami.exe"):
            return types.SimpleNamespace(stdout=b'"user","S-1-5-21-123"\r\n')
        assert Path(args[1]).name != "device.json"
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(keys.subprocess, "run", run)
    monkeypatch.setattr(keys.shutil, "which", lambda name: "icacls.exe" if name == "icacls" else "whoami.exe")
    keys.get_or_create()
    assert config.KEY_PATH.exists()
    assert any("*S-1-5-21-123:F" in args for args, _ in calls)


def test_windows_acl_failure_removes_key_and_does_not_decode_output(monkeypatch, tmp_path):
    monkeypatch.setattr(keys, "_keyring", lambda: None)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "KEY_PATH", tmp_path / "device.json")
    monkeypatch.setattr(keys, "os", types.SimpleNamespace(name="nt", replace=os.replace, environ={"SystemRoot": "C:\\Windows"}, path=os.path))
    monkeypatch.setattr(keys.shutil, "which", lambda name: "icacls.exe")

    def run(args, **kwargs):
        if args[0].lower().endswith("whoami.exe"):
            return types.SimpleNamespace(stdout=b'"user","S-1-5-21-123"\r\n')
        raise keys.subprocess.CalledProcessError(1, args, output=b"\xff")

    monkeypatch.setattr(keys.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="protect the device key"):
        keys.get_or_create()
    assert not config.KEY_PATH.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_remove_always_unlinks_key_when_keyring_delete_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "KEY_PATH", tmp_path / "device.json")
    config.KEY_PATH.write_text("{}", encoding="utf-8")

    class BrokenKeyring:
        errors = types.SimpleNamespace(KeyringError=RuntimeError)

        @staticmethod
        def get_password(service, username):
            raise RuntimeError("unavailable")

    monkeypatch.setattr(keys, "_keyring", lambda: BrokenKeyring)
    keys.remove()
    assert not config.KEY_PATH.exists()

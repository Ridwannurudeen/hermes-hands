import os
import types

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

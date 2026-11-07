import os

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


def test_screen_requires_explicit_flag():
    with pytest.raises(PermissionError, match="serve --allow-screen"):
        screen.capture(allow_screen=False)

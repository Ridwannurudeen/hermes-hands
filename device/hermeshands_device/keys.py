from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from . import config
from .protocol import b64u, generate_keypair, unb64u

SERVICE = "hermes-hands"


def _keyring():
    try:
        import keyring

        return keyring
    except ImportError:
        return None


def get_or_create() -> tuple[bytes, bytes]:
    keyring = _keyring()
    if keyring:
        value = keyring.get_password(SERVICE, "device-private")
        if value:
            private = unb64u(value)
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            from cryptography.hazmat.primitives import serialization

            public = Ed25519PrivateKey.from_private_bytes(private).public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            return private, public
    elif config.KEY_PATH.exists():
        value = json.loads(config.KEY_PATH.read_text(encoding="utf-8-sig"))
        return unb64u(value["private_key"]), unb64u(value["public_key"])
    private, public = generate_keypair()
    if keyring:
        keyring.set_password(SERVICE, "device-private", b64u(private))
    else:
        config.ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        config.KEY_PATH.write_text(json.dumps({"private_key": b64u(private), "public_key": b64u(public)}), encoding="utf-8")
        if os.name == "nt":
            icacls = shutil.which("icacls")
            if not icacls:
                config.KEY_PATH.unlink(missing_ok=True)
                raise RuntimeError("icacls is required to protect the device key")
            username = os.environ["USERNAME"]
            subprocess.run([icacls, str(config.KEY_PATH), "/inheritance:r", "/grant:r", f"{username}:F"], check=True, capture_output=True, text=True, encoding="utf-8")
        else:
            config.KEY_PATH.chmod(0o600)
    return private, public


def remove() -> None:
    keyring = _keyring()
    if keyring:
        if keyring.get_password(SERVICE, "device-private") is not None:
            keyring.delete_password(SERVICE, "device-private")
    config.KEY_PATH.unlink(missing_ok=True)

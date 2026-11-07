from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import config, executor, policy
from .keys import get_or_create, remove as remove_key
from .protocol import PROTOCOL_VERSION, R2D, b64u, canonical_json, fresh, sign, unb64u, verify_request
from . import service

API = "/api/plugins/hermes-hands"


def _pair(url: str, name: str, code: str) -> int:
    base_url = url.rstrip("/")
    if base_url.endswith(API):
        base_url = base_url[: -len(API)].rstrip("/")
    pair_url = base_url + API + "/pair"
    private, public = get_or_create()
    body = {"name": name, "os": platform.platform(), "home": str(Path.home()), "public_key": b64u(public), "policy_hash": policy.digest(), "protocol": PROTOCOL_VERSION}
    body["sig"] = b64u(sign(private, canonical_json(body)))
    response = httpx.post(pair_url, content=canonical_json(body), headers={"Authorization": f"Bearer {code}", "Content-Type": "application/json"}, timeout=30)
    if response.status_code != 200:
        print(f"Pairing failed: HTTP {response.status_code} {response.text}", file=sys.stderr)
        if response.status_code == 401:
            print("Check the gateway URL: use the base URL, e.g. https://hands.example.com", file=sys.stderr)
        return 1
    try:
        data = response.json()
        relay_key = unb64u(data["relay_public_key"])
        response_ts, response_nonce = int(response.headers["X-Hands-Ts"]), response.headers["X-Hands-Nonce"]
        response_path = urlparse(pair_url).path
        valid_signature = verify_request(relay_key, response.headers["X-Hands-Signature"], R2D, response_ts, response_nonce, "POST", response_path, response.content)
    except (KeyError, ValueError, TypeError):
        valid_signature = False
        data = {}
        relay_key = b""
        response_ts = 0
    if not re.fullmatch(r"[a-z0-9-]{1,40}", data.get("device_id", "")) or len(relay_key) != 32 or not fresh(response_ts, int(time.time())) or not valid_signature:
        print("Pairing failed: invalid relay signature", file=sys.stderr)
        return 1
    config.save({"gateway_url": base_url + API, "device_id": data["device_id"], "relay_public_key": data["relay_public_key"]})
    print(f"Paired as {data['device_id']}")
    return 0


def _doctor() -> int:
    failures = []
    bash = executor.resolve_bash()
    print(f"bash: {bash or 'missing'}")
    if not bash:
        failures.append("Git Bash")
    if bash:
        probe = subprocess.run([bash, "-c", "command -v python3 || command -v python"], capture_output=True, text=True, encoding="utf-8", timeout=5)
        print(f"python in bash: {probe.stdout.strip() or 'missing'}")
        if probe.returncode:
            failures.append("python in Git Bash")
    git_bash = shutil.which("git")
    print(f"git: {git_bash or 'missing'}")
    if sys.platform == "win32" and not git_bash:
        failures.append("git for Windows")
    try:
        import keyring
        print(f"keyring: available ({keyring.get_keyring().__class__.__name__})")
    except ImportError:
        print("keyring: unavailable; using protected file")
    url = config.gateway_url()
    if url:
        try:
            response = httpx.get(url, timeout=5)
            print(f"gateway: reachable (HTTP {response.status_code})")
        except httpx.HTTPError as exc:
            print(f"gateway: unreachable ({exc})")
            failures.append("gateway")
    else:
        print("gateway: not paired")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-hands")
    subs = parser.add_subparsers(dest="command", required=True)
    pair = subs.add_parser("pair")
    pair.add_argument("gateway_url")
    pair.add_argument("--name", required=True)
    pair.add_argument("--code", required=True)
    serve = subs.add_parser("serve")
    serve.add_argument("--allow-screen", action="store_true")
    subs.add_parser("status")
    subs.add_parser("doctor")
    install_service = subs.add_parser("install-service")
    install_service.add_argument("--dry-run", action="store_true")
    install_service.add_argument("--remove", action="store_true")
    pol = subs.add_parser("policy")
    pol.add_argument("action", choices=("show", "reset"))
    subs.add_parser("unpair")
    args = parser.parse_args(argv)
    if args.command == "pair":
        return _pair(args.gateway_url, args.name, args.code)
    if args.command == "serve":
        from .loop import DeviceLoop
        from .janitor import start as start_janitor

        if not config.device_id() or not config.gateway_url():
            parser.error("pair this device first")
        start_janitor()
        DeviceLoop(allow_screen=args.allow_screen).run_forever()
        return 0
    if args.command == "status":
        print(json.dumps({"paired": bool(config.device_id()), "device_id": config.device_id(), "gateway_url": config.gateway_url()}, indent=2))
        return 0
    if args.command == "doctor":
        return _doctor()
    if args.command == "install-service":
        return service.install(dry_run=args.dry_run, remove=args.remove)
    if args.command == "policy":
        if args.action == "show":
            policy.show()
        else:
            print(policy.reset())
        return 0
    if args.command == "unpair":
        config.save({})
        remove_key()
        print("Device unpaired")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

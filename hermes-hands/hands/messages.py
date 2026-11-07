"""Every user-facing string of the gateway half, in one place."""

from __future__ import annotations

UPGRADE = "pipx install hermes-hands==0.1.0"

RELAY_DOWN = (
    "hands: the relay (the hermes dashboard on the gateway host) is not reachable, so nothing ran on the device. "
    "Start the dashboard, or send /hands use server."
)
NOT_SET_UP = (
    "hands: the relay seed is missing, so no device can be reached. "
    "Run hermes hands setup on the gateway host, then restart the gateway and the dashboard."
)
RELAY_ELSEWHERE = (
    "hands: this process does not host the relay; it runs in the hermes dashboard process "
    "(dashboard_url in the hermes-hands settings)."
)
RELAY_LOST = (
    "hands: the relay restarted and lost this command; whether it ran on the device is unknown."
)
FORWARDED = "hands: local relay routes refuse proxied requests."
UNAUTHENTICATED = "hands: this request is not signed by a paired device."
UNKNOWN_DEVICE_WIRE = "hands: this device is not paired with the gateway (revoked or never paired)."
POLICY_CHANGED_WIRE = (
    "hands: the device deny-list changed since pairing; the owner must run /hands accept-policy."
)
BAD_REQUEST = "hands: malformed request body."
SCREENSHOT_TOO_LARGE = "hands: the screenshot is larger than 2 MiB; nothing was saved."
SCREENSHOT_FAILED = "hands: the device did not send a screenshot."
PAIR_BAD_CODE = "hands: this pairing code is unknown, expired or already used. Ask the owner for a new /hands pair code."
PAIR_BAD_SIGNATURE = "hands: the pairing request is not signed by the public key it carries."
TARGET_NOTE_SERVER = "hands: now on server (the gateway host)"
STORE_DOWN = (
    "hands: the Hermes Hands store (plugin-data/hermes-hands/hands.db) is unreadable, so this "
    "conversation's target is unknown and nothing ran anywhere. See the gateway log."
)
REPLAYED = "hands: this request was already used once."
RATE_LIMITED = (
    "hands: too many requests from unpaired devices at this address; try again in a minute."
)


def relay_refused(status: int) -> str:
    return (
        f"hands: the dashboard at dashboard_url answered HTTP {status}, so nothing ran on the device. "
        f"Either it has not loaded hermes-hands, or it runs with a different relay seed or HERMES_HOME "
        f"than this gateway; restart both after hermes hands setup and run hermes hands doctor."
    )


def offline(name: str) -> str:
    return (
        f"hands: {name} is offline, so nothing ran. Start hermes-hands serve on it, "
        f"or send /hands use server to run on the server."
    )


def unknown_device(name: str) -> str:
    return (
        f"hands: no paired device is named {name} (revoked or never paired), so nothing ran. "
        f"Send /hands use server, or pair it again with /hands pair {name}."
    )


def policy_changed(name: str) -> str:
    return (
        f"hands: the deny-list on {name} changed since it was paired, so the relay refuses it and nothing ran. "
        f"If you changed it yourself, run /hands accept-policy {name}."
    )


def went_offline(name: str) -> str:
    return f"hands: {name} went offline before the command finished; any output above is partial."


def revoked(name: str) -> str:
    return (
        f"hands: {name} was revoked while the command ran; the device stops it at its next request."
    )


def cancelled(name: str) -> str:
    return f"hands: the command on {name} was cancelled."


def pair_name_taken(name: str) -> str:
    return f"hands: a device named {name} is already paired. Revoke it first with /hands revoke {name}."


def pair_name_mismatch(minted: str) -> str:
    return f"hands: this pairing code was minted for a device named {minted}; pair with --name {minted}."


def protocol_mismatch(protocol: str) -> str:
    return f"hands: the device speaks {protocol}; this gateway needs a matching device agent: {UPGRADE}"


def target_note(name: str, device_os: str | None, home: str | None) -> str:
    """One line prefixed to the first terminal result on a device, so the model uses its paths."""
    if not device_os:
        return f"hands: now on {name}"
    if device_os.lower().startswith("windows"):
        return f"hands: now on {name} ({device_os}, Git Bash, home {home}; use /c/... paths)"
    return f"hands: now on {name} ({device_os}, home {home})"


def output_omitted(omitted: int, job_id: str | None) -> str:
    return (
        f"[hands: {omitted} bytes of output omitted here (inline_output_limit_bytes); the full output "
        f"is in ~/.hermes-hands/out/{job_id}.log on the device, which the device deny-list keeps "
        f"from the agent]"
    )


def alerts(devices: list[dict]) -> list[str]:
    """What /hands status and the Desktop pane show for devices the relay refuses. Hermes has no public
    way for a plugin to message the owner unprompted, so the alert waits to be looked at."""
    return [policy_changed(device["name"]) for device in devices if not device["policy_ok"]]

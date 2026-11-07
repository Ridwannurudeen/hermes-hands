"""The gateway's loopback client to the relay's ``/local/*`` routes (the relay runs in the dashboard).

``register()`` calls :func:`configure` once per process with ``dashboard_url`` and the local bearer
derived from the relay seed. Every refusal (device offline, revoked or policy-refused, relay down, no
seed) is raised as :class:`HandsError` whose text is the user-facing message.
"""

from __future__ import annotations

import base64
import binascii

import httpx

from . import messages
from .relay import API, MAX_OUTPUT_WAIT

_OUTPUT_TIMEOUT = MAX_OUTPUT_WAIT + 15
_SCREENSHOT_DEADLINE_POLLS = 6  # long-polls of MAX_OUTPUT_WAIT seconds each

_http: httpx.Client | None = None


class HandsError(Exception):
    """Nothing could be done on the device; ``str(exc)`` is the message for the user."""


def configure(dashboard_url: str, local_bearer: str | None) -> None:
    """Point the client at the relay; ``local_bearer`` None (no seed yet) makes every call refuse."""
    global _http
    _http = (
        None
        if local_bearer is None
        else httpx.Client(
            base_url=dashboard_url.rstrip("/") + API,
            headers={"Authorization": f"Bearer {local_bearer}"},
            timeout=_OUTPUT_TIMEOUT,
        )
    )


def _post(route: str, body: dict, *, timeout: float | None = None) -> dict:
    if _http is None:
        raise HandsError(messages.NOT_SET_UP)
    try:
        response = _http.post(route, json=body, **({"timeout": timeout} if timeout else {}))
    except httpx.HTTPError as exc:
        raise HandsError(messages.RELAY_DOWN) from exc
    try:
        data = response.json()
    except ValueError as exc:
        raise HandsError(messages.relay_refused(response.status_code)) from exc
    if response.status_code != 200:
        # The relay's own refusals carry a hands: detail; anything else (the token seam's bare 401,
        # a 404 for routes that are not mounted) means the dashboard is not the relay we expect.
        detail = str(data.get("detail") or "")
        raise HandsError(
            detail if detail.startswith("hands:") else messages.relay_refused(response.status_code)
        )
    return data


def start_job(device: str, *, script: str, login: bool, timeout: int) -> str:
    """Queue ``script`` for ``device``; returns the job id. Refused at once when the device is offline."""
    body = {"device": device, "type": "exec", "script": script, "login": login, "timeout": timeout}
    return str(_post("/local/exec", body)["job_id"])


def job_output(job_id: str, after: int) -> dict:
    """Long-poll the job: ``{"chunks", "next", "done", "returncode", "error", "png_b64"}``."""
    return _post("/local/output", {"job_id": job_id, "after": after, "wait": MAX_OUTPUT_WAIT})


def cancel_job(job_id: str) -> None:
    _post("/local/cancel", {"job_id": job_id}, timeout=5)


def state() -> dict:
    return _post("/local/state", {})


def request_screenshot(device_name: str) -> bytes:
    """Capture ``device_name``'s screen (the device must run ``serve --allow-screen``); PNG bytes."""
    job_id = str(_post("/local/exec", {"device": device_name, "type": "screenshot"})["job_id"])
    for _ in range(_SCREENSHOT_DEADLINE_POLLS):
        out = job_output(job_id, 0)
        if not out["done"]:
            continue
        if out.get("error") or not out.get("png_b64"):
            raise HandsError(
                str(out.get("error") or "".join(out["chunks"]) or messages.SCREENSHOT_FAILED)
            )
        try:
            return base64.b64decode(out["png_b64"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HandsError(messages.SCREENSHOT_FAILED) from exc
    cancel_job(job_id)
    raise HandsError(messages.SCREENSHOT_FAILED)

"""The gateway's loopback client to the relay's ``/local/*`` routes (the relay runs in the dashboard).

``register()`` calls :func:`configure` once per process with ``dashboard_url`` and the local bearer
derived from the relay seed. Every refusal (device offline, revoked or policy-refused, relay down, no
seed) is raised as :class:`HandsError` whose text is the user-facing message.
"""

from __future__ import annotations

import base64
import binascii
import time

import httpx

from . import messages
from .relay import API, MAX_OUTPUT_WAIT

_OUTPUT_TIMEOUT = MAX_OUTPUT_WAIT + 15
_OUTPUT_RETRIES = 2  # extra tries of an output fetch the relay did not answer
_RETRY_DELAY = 1.0
_SCREENSHOT_DEADLINE_POLLS = 6  # long-polls of MAX_OUTPUT_WAIT seconds each

_http: httpx.Client | None = None


class HandsError(Exception):
    """The relay refused or could not be asked; ``str(exc)`` is the message for the user."""


class RelayUnavailable(HandsError):
    """No usable answer from the relay (a transport error, a body that is not JSON, a 5xx), so an
    idempotent call may be tried again."""


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
        raise RelayUnavailable(messages.RELAY_DOWN) from exc
    try:
        data = response.json()
    except ValueError as exc:
        raise RelayUnavailable(messages.relay_refused(response.status_code)) from exc
    if response.status_code != 200:
        # The relay's own refusals carry a hands: detail; anything else (the token seam's bare 401,
        # a 404 for routes that are not mounted) means the dashboard is not the relay we expect.
        detail = str(data.get("detail") or "")
        error = RelayUnavailable if response.status_code >= 500 else HandsError
        raise error(
            detail if detail.startswith("hands:") else messages.relay_refused(response.status_code)
        )
    return data


def start_job(
    device: str, *, script: str, login: bool, timeout: int, stdin: str | None = None
) -> str:
    """Queue ``script`` for ``device``, with ``stdin`` as its standard input when given; returns the
    job id. Refused at once when the device is offline. Never retried: a lost reply may have queued it."""
    body = {"device": device, "type": "exec", "script": script, "login": login, "timeout": timeout}
    if stdin is not None:
        body["stdin"] = stdin
    return str(_post("/local/exec", body)["job_id"])


def job_output(job_id: str, after: int) -> dict:
    """Long-poll the job: ``{"chunks", "next", "done", "returncode", "error", "png_b64"}``. Tried again
    when the relay did not answer: the ``after`` cursor makes a repeat hand out the same chunks."""
    body = {"job_id": job_id, "after": after, "wait": MAX_OUTPUT_WAIT}
    for _ in range(_OUTPUT_RETRIES):
        try:
            return _post("/local/output", body)
        except RelayUnavailable:
            time.sleep(_RETRY_DELAY)
    return _post("/local/output", body)


def cancel_job(job_id: str) -> None:
    _post("/local/cancel", {"job_id": job_id}, timeout=5)


def state() -> dict:
    """What the relay shows; quick to give up, since /hands and the Desktop pane ask it often."""
    return _post("/local/state", {}, timeout=5)


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

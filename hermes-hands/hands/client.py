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
    idempotent call may be tried again. ``may_have_acted`` is True when the request was sent but no
    answer came back, so the relay may have done what it asked."""

    def __init__(self, message: str, *, may_have_acted: bool = False):
        super().__init__(message)
        self.may_have_acted = may_have_acted


# Only a connection failure proves that no request bytes left this process.
_NOT_SENT = (httpx.ConnectError,)


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
            trust_env=False,  # loopback only: a proxy must never see the bearer or the job bodies
        )
    )


def _post(route: str, body: dict, *, timeout: float | None = None) -> dict:
    if _http is None:
        raise HandsError(messages.NOT_SET_UP)
    try:
        response = _http.post(route, json=body, **({"timeout": timeout} if timeout else {}))
    except UnicodeEncodeError as exc:
        raise HandsError(messages.BAD_SCRIPT_ENCODING) from exc
    except httpx.HTTPError as exc:
        raise RelayUnavailable(
            messages.RELAY_DOWN, may_have_acted=not isinstance(exc, _NOT_SENT)
        ) from exc
    try:
        data = response.json()
    except ValueError as exc:
        raise RelayUnavailable(
            messages.relay_refused(response.status_code), may_have_acted=True
        ) from exc
    if response.status_code != 200:
        # The relay's own refusals carry a hands: detail; anything else (the token seam's bare 401,
        # a 404 for routes that are not mounted) means the dashboard is not the relay we expect.
        detail = str(data.get("detail") or "") if isinstance(data, dict) else ""
        message = (
            detail if detail.startswith("hands:") else messages.relay_refused(response.status_code)
        )
        if response.status_code >= 500:
            raise RelayUnavailable(message, may_have_acted=True)
        raise HandsError(message)
    if not isinstance(data, dict):
        raise RelayUnavailable(messages.relay_refused(response.status_code), may_have_acted=True)
    return data


def _stdin_fields(stdin: str | None) -> dict:
    """Text stdin stays a string; surrogateescaped bytes go as stdin_b64. Never both."""
    if stdin is None:
        return {}
    try:
        stdin.encode("utf-8")
    except UnicodeEncodeError:
        raw = stdin.encode("utf-8", errors="surrogateescape")
        return {"stdin_b64": base64.b64encode(raw).decode("ascii")}
    return {"stdin": stdin}


def start_job(
    device: str,
    *,
    script: str,
    login: bool,
    timeout: int,
    stdin: str | None = None,
    file_op: bool = False,
) -> str:
    """Queue a script with optional stdin and the file-tool policy marker."""
    body = {
        "device": device,
        "type": "exec",
        "script": script,
        "login": login,
        "timeout": timeout,
        **_stdin_fields(stdin),
        "file_op": file_op,
    }
    try:
        job_id = _post("/local/exec", body).get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise RelayUnavailable(messages.relay_refused(200), may_have_acted=True)
        return job_id
    except RelayUnavailable as exc:
        if exc.may_have_acted:
            raise HandsError(messages.queue_unknown(device)) from exc
        raise


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

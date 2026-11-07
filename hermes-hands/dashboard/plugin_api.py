"""Hermes Hands relay routes, mounted at ``/api/plugins/hermes-hands/`` (``manifest.json`` ``api``).

The dashboard imports this file on its own (``hermes_cli/web_server_dashboard.py:1233-1246``), not as
part of the plugin package, so it reaches the plugin through the token provider ``register()``
installed: that provider's package holds the seed-derived keys, and importing a second copy would
hold none. The gateway mounts these routes too (``gateway/run_api.py:128-162``); only the dashboard
process, where ``app.state.gateway_runner`` is None, serves as the relay.

Token routes (``relay.TOKEN_ROUTES``) are authenticated by the token seam before a handler runs;
``/ui/*`` are ordinary dashboard routes behind the session gate. Handlers are ``async`` and keep
blocking work on worker threads (the dashboard event loop is shared).
"""

from __future__ import annotations

import asyncio
import sys

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from hermes_cli.dashboard_auth import get_provider

router = APIRouter()

_NOT_LOADED = "hands: Hermes Hands is not loaded in this process."


def _plugin():
    """``(token provider, hands package)`` of the plugin loaded in this process, or None."""
    provider = get_provider("hermes-hands")
    if provider is None:
        return None
    return provider, sys.modules[type(provider).__module__.rpartition(".")[0]]


def _in_gateway(request: Request) -> bool:
    return getattr(request.app.state, "gateway_runner", None) is not None


def _response(reply) -> Response:
    return Response(
        content=reply.body,
        status_code=reply.status,
        headers=reply.headers,
        media_type="application/json" if reply.body else None,
    )


async def _relay_call(request: Request, handler: str) -> Response:
    plugin = _plugin()
    if plugin is None:
        return JSONResponse({"error": "not_loaded", "detail": _NOT_LOADED}, status_code=503)
    provider, hands = plugin
    served = hands.relay.serve(provider.keys, here=not _in_gateway(request))
    if isinstance(served, hands.relay.Reply):
        return _response(served)
    principal = getattr(request.state, "token_principal", None)
    body = await request.body()
    return _response(
        await getattr(served, handler)(principal, request.headers, request.url.path, body)
    )


@router.post("/pair")
async def pair(request: Request) -> Response:
    return await _relay_call(request, "pair")


@router.post("/device/poll")
async def device_poll(request: Request) -> Response:
    return await _relay_call(request, "device_poll")


@router.post("/device/result")
async def device_result(request: Request) -> Response:
    return await _relay_call(request, "device_result")


@router.post("/device/heartbeat")
async def device_heartbeat(request: Request) -> Response:
    return await _relay_call(request, "device_heartbeat")


@router.post("/local/exec")
async def local_exec(request: Request) -> Response:
    return await _relay_call(request, "local_exec")


@router.post("/local/output")
async def local_output(request: Request) -> Response:
    return await _relay_call(request, "local_output")


@router.post("/local/cancel")
async def local_cancel(request: Request) -> Response:
    return await _relay_call(request, "local_cancel")


@router.post("/local/state")
async def local_state(request: Request) -> Response:
    return await _relay_call(request, "local_state")


@router.get("/ui/state")
async def ui_state(request: Request) -> Response:
    """Desktop pane data. In the gateway's copy it is fetched from the relay over the local route."""
    plugin = _plugin()
    if plugin is None:
        return JSONResponse({"relay": False, "devices": [], "detail": _NOT_LOADED}, status_code=503)
    provider, hands = plugin
    if _in_gateway(request):
        try:
            return JSONResponse(await asyncio.to_thread(hands.client.state))
        except hands.client.HandsError as exc:
            return JSONResponse({"relay": False, "devices": [], "detail": str(exc)})
    served = hands.relay.serve(provider.keys, here=True)
    if isinstance(served, hands.relay.Reply):
        return _response(served)
    return JSONResponse(await served.state())

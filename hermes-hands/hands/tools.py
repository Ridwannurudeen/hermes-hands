"""Hands screenshot and device tools."""

from __future__ import annotations

import json
import uuid

from . import client, store, targets


def register_tools(ctx) -> None:
    def screenshot(_arguments: dict) -> str:
        try:
            if ctx.current_cron_execution() is not None:
                return json.dumps({"error": "Screenshots are unavailable in cron."})
            from gateway.session_context import get_session_env

            session_key = get_session_env("HERMES_SESSION_KEY", "")
            target = targets.resolve_target(
                session_key=session_key,
                in_cron=False,
                default_target=ctx.get_config("default_target", default="server"),
            )
            if target == "server":
                return json.dumps({"error": "Select a device with /hands use before requesting a screenshot."})
            if store.get_device(target) is None:
                return json.dumps({"error": f"No paired device named {target}."})
            png = client.request_screenshot(target)
            from hermes_constants import get_hermes_home

            output = get_hermes_home() / "cache" / "hermes-hands" / f"{uuid.uuid4()}.png"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(png)
            path = str(output.resolve())
            return json.dumps({"text": f"Screenshot saved. Include this exact line in your reply and call vision_analyze on the path.\nMEDIA:{path}"})
        except Exception as exc:
            return json.dumps({"error": str(exc) or "Screenshot failed."})

    def devices(_arguments: dict) -> str:
        try:
            return json.dumps({"devices": store.list_devices()})
        except Exception as exc:
            return json.dumps({"error": str(exc) or "Could not list devices."})

    ctx.register_tool(
        name="hands_screenshot",
        toolset="hands",
        schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=screenshot,
        description="Capture a screenshot from the selected paired device.",
    )
    ctx.register_tool(
        name="hands_devices",
        toolset="hands",
        schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=devices,
        description="List paired Hands devices and their status.",
    )

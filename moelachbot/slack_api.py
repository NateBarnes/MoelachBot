from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from typing import Any, Protocol

from .config import Config

REQUIRED_SCOPES = {
    "channels:history": "receive message events in public channels",
    "channels:read": "resolve channel names to IDs",
    "chat:write": "post messages",
}


class SlackClient(Protocol):
    """The three WebClient methods this bot actually uses.

    slack_sdk.WebClient satisfies this structurally; test doubles only need
    these three.
    """

    def auth_test(self) -> Any: ...

    def conversations_list(self, *, types: str, limit: int) -> Any: ...

    def chat_postMessage(self, *, channel: str, text: str) -> Any: ...


def check_scopes(client: SlackClient, log: logging.Logger) -> None:
    """Check that the bot token has all required OAuth scopes.

    Uses the x-oauth-scopes header from an auth.test call to determine
    the token's granted scopes. Logs a warning for each missing scope.
    """
    try:
        resp = client.auth_test()
        headers = resp.headers if hasattr(resp, "headers") else {}
        raw = headers.get("x-oauth-scopes", "")
        if not raw:
            log.warning(
                "Could not determine bot scopes from auth.test response headers — "
                "skipping scope validation"
            )
            return
        granted = {s.strip() for s in raw.split(",")}
        missing = {s for s in REQUIRED_SCOPES if s not in granted}
        if missing:
            log.warning("=" * 60)
            log.warning("MISSING REQUIRED OAUTH SCOPES")
            for scope in sorted(missing):
                log.warning("  - %s  (%s)", scope, REQUIRED_SCOPES[scope])
            log.warning(
                "Add the missing scope(s) at https://api.slack.com/apps → "
                "OAuth & Permissions, then reinstall the app to your workspace."
            )
            log.warning("=" * 60)
        else:
            log.info(
                "All required OAuth scopes are present: %s", ", ".join(sorted(granted))
            )
    except Exception:
        log.exception("Failed to validate OAuth scopes")


def resolve_channel_ids(
    client: SlackClient, names: Sequence[str], log: logging.Logger
) -> list[str]:
    """Convert channel names to channel IDs via the Slack API."""
    if not names:
        return []
    try:
        result = client.conversations_list(types="public_channel", limit=1000)
        name_to_id = {c["name"]: c["id"] for c in result["channels"]}
        resolved = []
        for name in names:
            if name.startswith("C"):  # already looks like an ID
                resolved.append(name)
            elif name in name_to_id:
                resolved.append(name_to_id[name])
                log.info("Resolved channel '%s' -> %s", name, name_to_id[name])
            else:
                log.warning("Could not resolve channel name '%s' — not found", name)
        return resolved
    except Exception:
        log.exception("Failed to resolve channel names, falling back to raw values")
        return list(names)


def resolve_config_channels(
    config: Config, client: SlackClient, log: logging.Logger
) -> Config:
    """Resolve channel names to IDs, returning a NEW Config.

    Replaces the original's `global MONITORED_CHANNELS, SCHEDULE_CHANNEL`
    rebinding. Safe to do before the bot exists because no events can arrive
    until SocketModeHandler.start(), the last statement of startup.
    """
    monitored = tuple(resolve_channel_ids(client, list(config.monitored_channels), log))
    schedule = config.schedule_channel
    if schedule:
        resolved = resolve_channel_ids(client, [schedule], log)
        schedule = resolved[0] if resolved else schedule
    return replace(config, monitored_channels=monitored, schedule_channel=schedule)

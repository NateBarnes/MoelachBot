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

# Enough for 50_000 channels at the page size below. A ceiling rather than a
# bare `while True`: a server that keeps handing back the same cursor would
# otherwise spin forever.
MAX_CHANNEL_PAGES = 50

# Named once: a half-fix that adds only groups:read makes the warning disappear
# while reactive posting stays dead, because the events never arrive.
PRIVATE_CHANNEL_HINT = (
    "resolving a private channel's ID needs the groups:read scope, and "
    "receiving its messages needs groups:history plus a message.groups "
    "event subscription"
)


class SlackClient(Protocol):
    """The three WebClient methods this bot actually uses.

    slack_sdk.WebClient satisfies this structurally; test doubles only need
    these three.
    """

    def auth_test(self) -> Any: ...

    def conversations_list(
        self, *, types: str, limit: int, cursor: str | None = None
    ) -> Any: ...

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


def _list_all_channels(
    client: SlackClient,
    log: logging.Logger,
    *,
    types: str = "public_channel",
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Every channel Slack will list for this token, following the cursor.

    `types` is a parameter so that enabling private channels later is a one-line
    change, but it has to stay "public_channel" by default: asking for
    private_channel without the groups:read scope fails the *whole* call with
    missing_scope rather than degrading to the public ones.
    """
    channels: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(MAX_CHANNEL_PAGES):
        result = client.conversations_list(types=types, limit=limit, cursor=cursor)
        # Subscripted, not .get(): a response with no "channels" key is
        # malformed, and that KeyError is what drops the caller into its
        # raw-names fallback. Extended into a fresh list, never aliased to the
        # response's own, which would mutate it across calls.
        channels.extend(result["channels"])
        # `or {}` rather than a .get default: Slack sends response_metadata as
        # null on some final pages, and None has no .get.
        next_cursor = (result.get("response_metadata") or {}).get("next_cursor") or ""
        if not next_cursor or next_cursor == cursor:
            return channels
        cursor = next_cursor
    log.warning(
        "Stopped paging conversations.list at %d pages; the channel list may be "
        "incomplete, so some names will not resolve",
        MAX_CHANNEL_PAGES,
    )
    return channels


def resolve_channel_ids(
    client: SlackClient, names: Sequence[str], log: logging.Logger
) -> list[str]:
    """Convert channel names to channel IDs via the Slack API.

    Returns one entry per input name, always. A name that cannot be resolved is
    passed through unchanged rather than dropped, because chat.postMessage
    accepts a channel *name* — so the raw value still posts fine, and the only
    caller that genuinely needs an ID (monitored channels, matched against the
    ID on an incoming event) can spot a passthrough by comparing input to
    output. Dropping used to make that caller silently receive an empty tuple.
    """
    if not names:
        return []
    try:
        name_to_id = {c["name"]: c["id"] for c in _list_all_channels(client, log)}
    except Exception:
        log.exception("Failed to resolve channel names, falling back to raw values")
        return list(names)

    # Outside the try: a failure in here is a bug, not an API problem, and
    # should not be reported as a resolution failure.
    resolved = []
    for name in names:
        if name.startswith("C"):  # already looks like an ID
            resolved.append(name)
        elif name in name_to_id:
            resolved.append(name_to_id[name])
            log.info("Resolved channel '%s' -> %s", name, name_to_id[name])
        else:
            resolved.append(name)
            log.info(
                "Channel '%s' is not among the %d channels this token can list "
                "— passing the name through to Slack, which accepts names",
                name,
                len(name_to_id),
            )
    return resolved


def resolve_config_channels(
    config: Config, client: SlackClient, log: logging.Logger
) -> Config:
    """Resolve channel names to IDs, returning a NEW Config.

    Replaces the original's `global MONITORED_CHANNELS, SCHEDULE_CHANNEL`
    rebinding. Safe to do before the bot exists because no events can arrive
    until SocketModeHandler.start(), the last statement of startup.
    """
    names = list(config.monitored_channels)
    monitored = resolve_channel_ids(client, names, log)
    # An unresolved *monitored* name is the one case that actually breaks
    # something: handle_message compares against the ID on the incoming event,
    # so a bare name can never match and reactive posting silently dies. The
    # posting paths cope with a raw name, which is why the resolver itself only
    # logs at INFO and the warning lives here, where the consequence is known.
    for name, resolved in zip(names, monitored):
        if resolved == name and not name.startswith("C"):
            log.warning(
                "Monitored channel '%s' did not resolve to an ID, so reactive "
                "posting will never fire for it — %s",
                name,
                PRIVATE_CHANNEL_HINT,
            )

    schedule = config.schedule_channel
    if schedule:
        schedule = resolve_channel_ids(client, [schedule], log)[0]
    return replace(
        config, monitored_channels=tuple(monitored), schedule_channel=schedule
    )

import json
import logging
import os
import random
import threading
import time
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from death_rattles import DEATH_RATTLES

load_dotenv()

log = logging.getLogger("moelachbot")
log.setLevel(logging.DEBUG)

_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

_console = logging.StreamHandler()
_console.setLevel(logging.DEBUG)
_console.setFormatter(_formatter)
log.addHandler(_console)

_logfile = logging.FileHandler(Path(__file__).parent / "moelachbot.log")
_logfile.setLevel(logging.DEBUG)
_logfile.setFormatter(_formatter)
log.addHandler(_logfile)

STATE_FILE = Path(__file__).parent / "state.json"

# ---------------------------------------------------------------------------
# Configuration
# NOTE: Required OAuth scopes:
#   - channels:history  (receive message events in public channels)
#   - channels:read     (resolve channel names to IDs)
#   - chat:write        (post messages)
# Under Event Subscriptions → Subscribe to bot events, enable:
#   message.channels
# ---------------------------------------------------------------------------

REQUIRED_SCOPES = {
    "channels:history": "receive message events in public channels",
    "channels:read": "resolve channel names to IDs",
    "chat:write": "post messages",
}


def _check_scopes(app: App) -> None:
    """Check that the bot token has all required OAuth scopes.

    Uses the x-oauth-scopes header from an auth.test call to determine
    the token's granted scopes. Logs a warning for each missing scope.
    """
    try:
        resp = app.client.auth_test()
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
                log.warning(
                    "  - %s  (%s)", scope, REQUIRED_SCOPES[scope]
                )
            log.warning(
                "Add the missing scope(s) at https://api.slack.com/apps → "
                "OAuth & Permissions, then reinstall the app to your workspace."
            )
            log.warning("=" * 60)
        else:
            log.info("All required OAuth scopes are present: %s", ", ".join(sorted(granted)))
    except Exception:
        log.exception("Failed to validate OAuth scopes")

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]

# Reactive posting
MONITORED_CHANNELS = [
    c.strip()
    for c in os.getenv("MONITORED_CHANNELS", "").split(",")
    if c.strip()
]
COOLDOWN_HOURS = float(os.getenv("COOLDOWN_HOURS", "48"))
RANDOM_DELAY_MAX_MINUTES = int(os.getenv("RANDOM_DELAY_MAX_MINUTES", "180"))

# Scheduled posting
SCHEDULE_CHANNEL = os.getenv("SCHEDULE_CHANNEL", "")
SCHEDULE_DAY = os.getenv("SCHEDULE_DAY", "wed")
SCHEDULE_HOUR = int(os.getenv("SCHEDULE_HOUR", "10"))
SCHEDULE_MINUTE = int(os.getenv("SCHEDULE_MINUTE", "0"))
SCHEDULE_TIMEZONE = os.getenv("SCHEDULE_TIMEZONE", "America/Los_Angeles")


def _resolve_channel_ids(app: App, names: list[str]) -> list[str]:
    """Convert channel names to channel IDs via the Slack API."""
    if not names:
        return []
    try:
        result = app.client.conversations_list(
            types="public_channel", limit=1000
        )
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
        return names


# ---------------------------------------------------------------------------
# State helpers  (tracks cooldowns + used-rattle indices)
# ---------------------------------------------------------------------------


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_reactive": {}, "used_indices": []}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _pick_rattle(state: dict) -> str:
    """Pick a random Death Rattle, cycling through all before repeating."""
    used = state.get("used_indices", [])
    available = [i for i in range(len(DEATH_RATTLES)) if i not in used]
    if not available:
        used.clear()
        available = list(range(len(DEATH_RATTLES)))
    idx = random.choice(available)
    used.append(idx)
    state["used_indices"] = used
    return DEATH_RATTLES[idx]


def _format_rattle(rattle: str) -> str:
    return f"_{rattle}_"


# ---------------------------------------------------------------------------
# Reactive posting
# ---------------------------------------------------------------------------


def _channel_on_cooldown(channel: str, state: dict) -> bool:
    last = state.get("last_reactive", {}).get(channel)
    if last is None:
        return False
    elapsed_hours = (time.time() - last) / 3600
    return elapsed_hours < COOLDOWN_HOURS


def _schedule_reactive_post(app: App, channel: str) -> None:
    """Wait a random delay then post a rattle to the channel."""
    delay_minutes = random.uniform(0, RANDOM_DELAY_MAX_MINUTES)
    delay_seconds = delay_minutes * 60
    log.info("Scheduling reactive post to %s in %.1f minutes", channel, delay_minutes)

    def _post():
        try:
            time.sleep(delay_seconds)
            state = _load_state()
            # Re-check cooldown in case another post happened while waiting
            if _channel_on_cooldown(channel, state):
                log.info("Post to %s cancelled — channel went on cooldown during delay", channel)
                return
            rattle = _pick_rattle(state)
            app.client.chat_postMessage(channel=channel, text=_format_rattle(rattle))
            state.setdefault("last_reactive", {})[channel] = time.time()
            _save_state(state)
            log.info("Posted reactive rattle to %s", channel)
        except Exception:
            log.exception("Error posting reactive rattle to %s", channel)

    t = threading.Thread(target=_post, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Scheduled posting
# ---------------------------------------------------------------------------


def _scheduled_post(app: App) -> None:
    try:
        state = _load_state()
        rattle = _pick_rattle(state)
        _save_state(state)
        app.client.chat_postMessage(
            channel=SCHEDULE_CHANNEL, text=_format_rattle(rattle)
        )
        log.info("Posted scheduled rattle to %s", SCHEDULE_CHANNEL)
    except Exception:
        log.exception("Error posting scheduled rattle to %s", SCHEDULE_CHANNEL)


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = App(token=SLACK_BOT_TOKEN)


@app.event("message")
def handle_message(event, say):
    # Ignore bot messages and message edits/deletes
    if event.get("subtype"):
        log.debug("Ignoring message with subtype=%s", event.get("subtype"))
        return
    channel = event.get("channel", "")
    if channel not in MONITORED_CHANNELS:
        log.debug("Ignoring message in non-monitored channel %s", channel)
        return
    state = _load_state()
    if _channel_on_cooldown(channel, state):
        log.info("Channel %s is on cooldown — skipping", channel)
        return
    log.info("Triggering reactive post for channel %s", channel)
    _schedule_reactive_post(app, channel)


def main():
    global MONITORED_CHANNELS, SCHEDULE_CHANNEL

    # Validate that the bot token has the scopes we need
    _check_scopes(app)

    # Resolve human-readable channel names to Slack channel IDs
    MONITORED_CHANNELS = _resolve_channel_ids(app, MONITORED_CHANNELS)
    if SCHEDULE_CHANNEL:
        resolved = _resolve_channel_ids(app, [SCHEDULE_CHANNEL])
        SCHEDULE_CHANNEL = resolved[0] if resolved else SCHEDULE_CHANNEL

    log.info("Starting MoelachBot")
    log.info("  Monitored channels: %s", MONITORED_CHANNELS or "(none)")
    log.info("  Cooldown: %.1f hours", COOLDOWN_HOURS)
    log.info("  Random delay max: %d minutes", RANDOM_DELAY_MAX_MINUTES)
    log.info("  Schedule channel: %s", SCHEDULE_CHANNEL or "(none)")

    # Set up scheduled posting if configured
    if SCHEDULE_CHANNEL:
        scheduler = BackgroundScheduler()
        trigger = CronTrigger(
            day_of_week=SCHEDULE_DAY,
            hour=SCHEDULE_HOUR,
            minute=SCHEDULE_MINUTE,
            timezone=SCHEDULE_TIMEZONE,
        )
        scheduler.add_job(_scheduled_post, trigger, args=[app])
        scheduler.start()
        log.info(
            "  Scheduled posting: every %s at %02d:%02d %s",
            SCHEDULE_DAY, SCHEDULE_HOUR, SCHEDULE_MINUTE, SCHEDULE_TIMEZONE,
        )

    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    log.info("MoelachBot is running")
    handler.start()


if __name__ == "__main__":
    main()

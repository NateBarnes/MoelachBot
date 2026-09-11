from __future__ import annotations

import argparse
import logging
import random
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable, TextIO

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from .clock import Clock, SystemClock
from .config import Config, Paths
from .core import MoelachBot
from .rattles import format_rattle, load_death_rattles
from .runner import ThreadTaskRunner
from .slack_api import check_scopes, resolve_channel_ids, resolve_config_channels
from .state import StateStore

LOGGER_NAME = "moelachbot"


def setup_logging(log_path: Path, name: str = LOGGER_NAME) -> logging.Logger:
    """Console + file logging. Idempotent, so a second call cannot double up
    handlers the way a module reload used to."""
    log = logging.getLogger(name)
    if log.handlers:
        return log
    log.setLevel(logging.DEBUG)

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG)
    console.setFormatter(formatter)
    log.addHandler(console)

    logfile = logging.FileHandler(log_path)
    logfile.setLevel(logging.DEBUG)
    logfile.setFormatter(formatter)
    log.addHandler(logfile)
    return log


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MoelachBot")
    parser.add_argument(
        "--reset-cooldown",
        nargs="?",
        const="__all__",
        metavar="CHANNEL",
        help="Clear cooldown for CHANNEL (name or ID), or all channels if omitted",
    )
    parser.add_argument(
        "--send",
        metavar="MESSAGE",
        help="Send MESSAGE to the schedule channel (or --channel) and exit",
    )
    parser.add_argument(
        "--channel",
        metavar="CHANNEL",
        help="Override target channel for --send (default: SCHEDULE_CHANNEL)",
    )
    return parser


def build_app(config: Config) -> App:
    return App(token=config.slack_bot_token)


def reset_cooldown(
    store: StateStore,
    channel: str | None,
    clock: Clock,
    out: TextIO | None = None,
) -> None:
    """Clear the cooldown for a channel, or all channels if none specified.

    Writes to stdout, not the log: this output is a CLI contract.
    """
    # Resolved at call time, not bound as a default, so capsys can swap stdout.
    out = sys.stdout if out is None else out
    state = store.load()
    if channel:
        if channel in state.get("last_reactive", {}):
            del state["last_reactive"][channel]
            print(f"Cooldown cleared for channel: {channel}", file=out)
        else:
            print(f"No cooldown found for channel: {channel}", file=out)
            existing = state.get("last_reactive", {})
            if existing:
                print("Current cooldowns:", file=out)
                for cid, ts in existing.items():
                    print(f"  {cid}: {clock.ctime(ts)}", file=out)
            else:
                print("No cooldowns are currently set.", file=out)
    else:
        state["last_reactive"] = {}
        print("All cooldowns cleared.", file=out)
    store.save(state)


def register_handlers(app: Any, bot: MoelachBot) -> None:
    # Bolt injects listener arguments by parameter NAME, so `event` and `say`
    # must keep their names even though `say` goes unused.
    @app.event("message")
    def handle_message(event, say):  # noqa: ARG001
        bot.handle_message(event)


def run_bot(
    app: Any,
    config: Config,
    paths: Paths,
    store: StateStore,
    clock: Clock,
    log: logging.Logger,
    *,
    scheduler_factory: Callable[[], Any] = BackgroundScheduler,
    handler_factory: Callable[[Any, str], Any] = SocketModeHandler,
) -> None:
    # Validate that the bot token has the scopes we need
    check_scopes(app.client, log)

    # Resolve human-readable channel names to Slack channel IDs
    config = resolve_config_channels(config, app.client, log)

    bot = MoelachBot(
        config=config,
        client=app.client,
        rattles=load_death_rattles(paths.rattles),
        state_store=store,
        clock=clock,
        rng=random.Random(),
        runner=ThreadTaskRunner(),
        log=log,
    )
    register_handlers(app, bot)

    log.info("Starting MoelachBot")
    # list(...) so the banner prints ['C1'] as it always has, not ('C1',)
    log.info("  Monitored channels: %s", list(config.monitored_channels) or "(none)")
    log.info("  Cooldown: %.1f hours", config.cooldown_hours)
    log.info("  Random delay max: %d minutes", config.random_delay_max_minutes)
    log.info("  Schedule channel: %s", config.schedule_channel or "(none)")
    if config.schedule_rattle:
        log.info("  Scheduled rattle: #%s", config.schedule_rattle)

    # Set up scheduled posting if configured
    if config.schedule_channel:
        scheduler = scheduler_factory()
        trigger = CronTrigger(
            day_of_week=config.schedule_day,
            hour=config.schedule_hour,
            minute=config.schedule_minute,
            timezone=config.schedule_timezone,
        )
        scheduler.add_job(bot.post_scheduled, trigger)
        scheduler.start()
        log.info(
            "  Scheduled posting: every %s at %02d:%02d %s",
            config.schedule_day,
            config.schedule_hour,
            config.schedule_minute,
            config.schedule_timezone,
        )

    handler = handler_factory(app, config.slack_app_token)
    log.info("MoelachBot is running")
    handler.start()


def main(
    argv: Sequence[str] | None = None,
    *,
    base_dir: Path,
    env: Mapping[str, str] | None = None,
    clock: Clock | None = None,
    app_factory: Callable[[Config], Any] = build_app,
    scheduler_factory: Callable[[], Any] = BackgroundScheduler,
    handler_factory: Callable[[Any, str], Any] = SocketModeHandler,
) -> None:
    args = build_parser().parse_args(argv)

    paths = Paths.from_base(base_dir)
    config = Config.from_env(env)
    log = setup_logging(paths.log)
    store = StateStore(paths.state)
    clock = SystemClock() if clock is None else clock

    if args.reset_cooldown:
        channel = None if args.reset_cooldown == "__all__" else args.reset_cooldown
        reset_cooldown(store, channel, clock)
        return

    app = app_factory(config)

    if args.send:
        channel = args.channel or config.schedule_channel
        if not channel:
            print("Error: no channel specified. Set SCHEDULE_CHANNEL or use --channel.")
            return
        resolved = resolve_channel_ids(app.client, [channel], log)
        target = resolved[0] if resolved else channel
        app.client.chat_postMessage(channel=target, text=format_rattle(args.send))
        print(f"Sent to {target}: {format_rattle(args.send)}")
        return

    run_bot(
        app,
        config,
        paths,
        store,
        clock,
        log,
        scheduler_factory=scheduler_factory,
        handler_factory=handler_factory,
    )

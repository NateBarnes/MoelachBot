from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Paths:
    """Filesystem anchors, all derived from one base directory.

    Kept together so tests can point the whole bot at a tmp_path with one call.
    """

    rattles: Path
    state: Path
    log: Path

    @classmethod
    def from_base(cls, base: Path) -> "Paths":
        return cls(
            rattles=base / "death_rattles.md",
            state=base / "state.json",
            log=base / "moelachbot.log",
        )


@dataclass(frozen=True, slots=True)
class Config:
    slack_bot_token: str
    slack_app_token: str
    monitored_channels: tuple[str, ...]
    cooldown_hours: float
    random_delay_max_minutes: int
    schedule_channel: str
    schedule_day: str
    schedule_hour: int
    schedule_minute: int
    schedule_timezone: str
    # Left as a raw string: parsing is deferred to post time, where the original
    # code's int() failure is swallowed and logged rather than crashing startup.
    schedule_rattle: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        # env[...] rather than .get(): a missing token must still raise KeyError.
        # env.get(k, default) rather than `env.get(k) or default`: a set-but-empty
        # COOLDOWN_HOURS must still raise ValueError, as it does today.
        return cls(
            slack_bot_token=env["SLACK_BOT_TOKEN"],
            slack_app_token=env["SLACK_APP_TOKEN"],
            monitored_channels=tuple(
                c.strip()
                for c in env.get("MONITORED_CHANNELS", "").split(",")
                if c.strip()
            ),
            cooldown_hours=float(env.get("COOLDOWN_HOURS", "48")),
            random_delay_max_minutes=int(env.get("RANDOM_DELAY_MAX_MINUTES", "180")),
            schedule_channel=env.get("SCHEDULE_CHANNEL", ""),
            schedule_day=env.get("SCHEDULE_DAY", "wed"),
            schedule_hour=int(env.get("SCHEDULE_HOUR", "10")),
            schedule_minute=int(env.get("SCHEDULE_MINUTE", "0")),
            schedule_timezone=env.get("SCHEDULE_TIMEZONE", "America/Los_Angeles"),
            schedule_rattle=env.get("SCHEDULE_RATTLE", ""),
        )

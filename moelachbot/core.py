from __future__ import annotations

import logging
import random
from collections.abc import Mapping, Sequence
from typing import Any

from .clock import Clock
from .config import Config
from .rattles import format_rattle, pick_rattle
from .runner import TaskRunner
from .slack_api import SlackClient
from .state import StateStore, channel_on_cooldown


class MoelachBot:
    """The bot's behavior, with every collaborator injected.

    Takes the Slack WebClient rather than the Bolt App, because `app.client` is
    the only attribute any of this code touches.
    """

    def __init__(
        self,
        *,
        config: Config,
        client: SlackClient,
        rattles: Sequence[str],
        state_store: StateStore,
        clock: Clock,
        rng: random.Random,
        runner: TaskRunner,
        log: logging.Logger,
    ) -> None:
        self.config = config
        self.client = client
        self.rattles = rattles
        self.state_store = state_store
        self.clock = clock
        self.rng = rng
        self.runner = runner
        self.log = log

    # ---- reactive -------------------------------------------------------

    def handle_message(self, event: Mapping[str, Any]) -> None:
        # No try/except here, matching the original: a corrupt state file
        # propagates into the Bolt listener rather than being silently eaten.
        if event.get("subtype"):
            self.log.debug("Ignoring message with subtype=%s", event.get("subtype"))
            return
        channel = event.get("channel", "")
        if channel not in self.config.monitored_channels:
            self.log.debug("Ignoring message in non-monitored channel %s", channel)
            return
        state = self.state_store.load()
        if self.channel_on_cooldown(channel, state):
            self.log.info("Channel %s is on cooldown — skipping", channel)
            return
        self.log.info("Triggering reactive post for channel %s", channel)
        self.schedule_reactive_post(channel)

    def schedule_reactive_post(self, channel: str) -> None:
        """Hand a delayed post to the runner. Not wrapped in try/except,
        matching the original."""
        delay_minutes = self.rng.uniform(0, self.config.random_delay_max_minutes)
        delay_seconds = delay_minutes * 60
        self.log.info(
            "Scheduling reactive post to %s in %.1f minutes", channel, delay_minutes
        )
        self.runner.submit(lambda: self.post_reactive(channel, delay_seconds))

    def post_reactive(self, channel: str, delay_seconds: float) -> None:
        """Wait out the delay, then post — unless the channel went on cooldown
        while we waited.

        The try must span the sleep: that is where the original put it.
        """
        try:
            self.clock.sleep(delay_seconds)
            state = self.state_store.load()
            # Re-check cooldown in case another post happened while waiting
            if self.channel_on_cooldown(channel, state):
                self.log.info(
                    "Post to %s cancelled — channel went on cooldown during delay",
                    channel,
                )
                return
            rattle = pick_rattle(self.rattles, state, self.rng)
            self.client.chat_postMessage(channel=channel, text=format_rattle(rattle))
            state.setdefault("last_reactive", {})[channel] = self.clock.now()
            self.state_store.save(state)
            self.log.info("Posted reactive rattle to %s", channel)
        except Exception:
            self.log.exception("Error posting reactive rattle to %s", channel)

    # ---- scheduled ------------------------------------------------------

    def post_scheduled(self) -> None:
        try:
            if self.config.schedule_rattle:
                idx = int(self.config.schedule_rattle) - 1
                rattle = self.rattles[idx]
            else:
                state = self.state_store.load()
                rattle = pick_rattle(self.rattles, state, self.rng)
                self.state_store.save(state)
            self.client.chat_postMessage(
                channel=self.config.schedule_channel, text=format_rattle(rattle)
            )
            self.log.info(
                "Posted scheduled rattle to %s", self.config.schedule_channel
            )
        except Exception:
            self.log.exception(
                "Error posting scheduled rattle to %s", self.config.schedule_channel
            )

    # ---- shared ---------------------------------------------------------

    def channel_on_cooldown(self, channel: str, state: dict[str, Any]) -> bool:
        return channel_on_cooldown(
            channel,
            state,
            now=self.clock.now(),
            cooldown_hours=self.config.cooldown_hours,
        )

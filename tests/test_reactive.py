"""Tests for the reactive path: ``handle_message`` -> ``schedule_reactive_post``
-> ``post_reactive``.

``post_reactive`` used to be untestable: it slept a random number of minutes on
a daemon thread and then talked to Slack. Here the clock, the RNG and the thread
are all injected, so the interesting race -- "the channel went on cooldown while
we were asleep" -- is exercised deterministically via ``FakeClock.on_sleep``.

Several assertions below document behavior that is surprising but deliberate
(an empty-string subtype is *not* ignored; ``handle_message`` lets a corrupt
state file escape). Those are marked inline so a future reader does not "fix"
the test.
"""

from __future__ import annotations

import json
import logging

import pytest

from moelachbot.runner import ThreadTaskRunner
from moelachbot.state import StateStore

# The monitored channel configured by the `config` fixture.
CHANNEL = "C_MON"
OTHER_CHANNEL = "C_OTHER"


def messages(caplog, level: int = logging.DEBUG) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= level]


def errors(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


class ExplodingSaveStore(StateStore):
    """Loads normally, refuses to save. The disk-full / read-only-fs case."""

    def save(self, state):
        raise OSError("disk full")


# ------------------------------------------------------------ handle_message


@pytest.mark.parametrize(
    "subtype",
    [
        "bot_message",
        "message_changed",
        "message_deleted",
        "channel_join",
        "thread_broadcast",
    ],
)
def test_handle_message_ignores_messages_with_a_subtype(
    bot, slack_client, caplog, subtype
):
    bot.handle_message({"subtype": subtype, "channel": CHANNEL})

    slack_client.chat_postMessage.assert_not_called()
    assert f"Ignoring message with subtype={subtype}" in messages(caplog)


def test_handle_message_does_not_ignore_an_empty_string_subtype(bot, slack_client):
    # Deliberate: the guard is `if event.get("subtype")`, so a *falsy* subtype
    # (the empty string) sails straight through and the bot posts. Documented,
    # not endorsed.
    bot.handle_message({"subtype": "", "channel": CHANNEL})

    slack_client.chat_postMessage.assert_called_once_with(
        channel=CHANNEL, text="_first rattle_"
    )


def test_handle_message_ignores_non_monitored_channels(bot, slack_client, caplog):
    bot.handle_message({"channel": "C_SOMEWHERE_ELSE"})

    slack_client.chat_postMessage.assert_not_called()
    assert "Ignoring message in non-monitored channel C_SOMEWHERE_ELSE" in messages(
        caplog
    )


def test_handle_message_without_a_channel_key_is_ignored(bot, slack_client, caplog):
    # A missing "channel" defaults to "", which is never in monitored_channels.
    bot.handle_message({})

    slack_client.chat_postMessage.assert_not_called()
    assert "Ignoring message in non-monitored channel " in messages(caplog)


def test_handle_message_skips_a_channel_on_cooldown(
    bot, clock, write_state, slack_client, mocker, caplog
):
    write_state({"last_reactive": {CHANNEL: clock.now()}, "used_indices": []})
    # Spy, because "no post happened" alone cannot distinguish returning early
    # from scheduling a post that then cancels itself on the mid-delay re-check.
    # Without this, dropping the early return spawns a thread per message in
    # production and no test notices.
    spy = mocker.spy(bot, "schedule_reactive_post")

    bot.handle_message({"channel": CHANNEL})

    spy.assert_not_called()
    slack_client.chat_postMessage.assert_not_called()
    assert f"Channel {CHANNEL} is on cooldown — skipping" in messages(
        caplog, logging.INFO
    )
    assert f"Triggering reactive post for channel {CHANNEL}" not in messages(caplog)


def test_handle_message_schedules_a_post_when_not_on_cooldown(
    bot, fresh_state, mocker, caplog
):
    spy = mocker.spy(bot, "schedule_reactive_post")

    bot.handle_message({"channel": CHANNEL})

    spy.assert_called_once_with(CHANNEL)
    assert f"Triggering reactive post for channel {CHANNEL}" in messages(
        caplog, logging.INFO
    )


def test_handle_message_does_not_post_synchronously(
    make_bot, deferred_runner, slack_client
):
    bot = make_bot(runner=deferred_runner)

    bot.handle_message({"channel": CHANNEL})

    # The work is queued on the runner (the thread seam); nothing has posted yet.
    assert len(deferred_runner.jobs) == 1
    slack_client.chat_postMessage.assert_not_called()


def test_handle_message_never_triggers_when_no_channels_are_monitored(
    make_bot, deferred_runner, slack_client
):
    bot = make_bot(
        config_overrides={"monitored_channels": ()}, runner=deferred_runner
    )

    for channel in (CHANNEL, OTHER_CHANNEL, ""):
        bot.handle_message({"channel": channel})

    assert deferred_runner.jobs == []
    slack_client.chat_postMessage.assert_not_called()


def test_handle_message_lets_a_corrupt_state_file_escape(bot, state_path):
    # Deliberate: handle_message has NO try/except, so a corrupt state.json
    # propagates into the Bolt listener instead of being silently eaten.
    # This is the current contract and the test exists to preserve it.
    state_path.write_text("{not json at all")

    with pytest.raises(json.JSONDecodeError):
        bot.handle_message({"channel": CHANNEL})


# ----------------------------------------------------- schedule_reactive_post


def test_schedule_reactive_post_draws_the_delay_from_the_configured_range(
    bot, stub_rng
):
    bot.schedule_reactive_post(CHANNEL)

    assert stub_rng.uniform_calls == [(0, 180)]


def test_schedule_reactive_post_logs_the_delay_in_minutes(bot, stub_rng, caplog):
    stub_rng.uniform_value = 2.5

    bot.schedule_reactive_post(CHANNEL)

    assert f"Scheduling reactive post to {CHANNEL} in 2.5 minutes" in messages(
        caplog, logging.INFO
    )


def test_schedule_reactive_post_hands_the_work_to_the_runner(
    make_bot, deferred_runner, slack_client
):
    bot = make_bot(runner=deferred_runner)

    bot.schedule_reactive_post(CHANNEL)

    assert len(deferred_runner.jobs) == 1
    slack_client.chat_postMessage.assert_not_called()

    deferred_runner.run_pending()

    slack_client.chat_postMessage.assert_called_once_with(
        channel=CHANNEL, text="_first rattle_"
    )


def test_schedule_reactive_post_with_a_zero_max_delay_sleeps_zero(make_bot, clock):
    bot = make_bot(config_overrides={"random_delay_max_minutes": 0})

    bot.schedule_reactive_post(CHANNEL)

    assert clock.sleeps == [0.0]


def test_schedule_reactive_post_converts_minutes_to_seconds(bot, stub_rng, clock):
    stub_rng.uniform_value = 2.5

    bot.schedule_reactive_post(CHANNEL)

    assert clock.sleeps == [150.0]


# ------------------------------------------------------------- post_reactive


def test_post_reactive_sleeps_before_posting(bot, clock, slack_client):
    posts_during_sleep = []
    clock.on_sleep = lambda _c: posts_during_sleep.append(
        slack_client.chat_postMessage.call_count
    )

    bot.post_reactive(CHANNEL, 90.0)

    assert clock.sleeps == [90.0]
    assert posts_during_sleep == [0]
    assert slack_client.chat_postMessage.call_count == 1


def test_post_reactive_posts_the_italicised_rattle(bot, slack_client, caplog):
    bot.post_reactive(CHANNEL, 1.0)

    slack_client.chat_postMessage.assert_called_once_with(
        channel=CHANNEL, text="_first rattle_"
    )
    assert f"Posted reactive rattle to {CHANNEL}" in messages(caplog, logging.INFO)


def test_post_reactive_records_the_post_sleep_timestamp(bot, clock, read_state):
    started_at = clock.now()

    bot.post_reactive(CHANNEL, 60.0)

    assert read_state()["last_reactive"][CHANNEL] == clock.now() == started_at + 60.0


def test_post_reactive_persists_state_to_disk(bot, clock, read_state):
    bot.post_reactive(CHANNEL, 60.0)

    assert read_state() == {
        "last_reactive": {CHANNEL: clock.now()},
        "used_indices": [0],
    }


def test_post_reactive_loads_state_after_the_sleep_not_before(
    bot, clock, write_state, read_state, slack_client
):
    write_state({"last_reactive": {}, "used_indices": []})
    clock.on_sleep = lambda _c: write_state(
        {"last_reactive": {}, "used_indices": [0, 1]}
    )

    bot.post_reactive(CHANNEL, 30.0)

    # Index 2 is the only one still available in the state written *during* the
    # sleep. Had the state been read before sleeping, index 0 would have won.
    slack_client.chat_postMessage.assert_called_once_with(
        channel=CHANNEL, text="_third rattle_"
    )
    assert read_state()["used_indices"] == [0, 1, 2]


def test_post_reactive_aborts_when_the_channel_goes_on_cooldown_mid_delay(
    bot, clock, write_state, slack_client, caplog
):
    write_state({"last_reactive": {}, "used_indices": []})
    clock.on_sleep = lambda c: write_state(
        {"last_reactive": {CHANNEL: c.now()}, "used_indices": []}
    )

    bot.post_reactive(CHANNEL, 120.0)

    slack_client.chat_postMessage.assert_not_called()
    assert (
        f"Post to {CHANNEL} cancelled — channel went on cooldown during delay"
        in messages(caplog, logging.INFO)
    )


def test_post_reactive_abort_does_not_consume_a_rattle(
    bot, clock, write_state, read_state
):
    write_state({"last_reactive": {}, "used_indices": [0]})
    clock.on_sleep = lambda c: write_state(
        {"last_reactive": {CHANNEL: c.now()}, "used_indices": [0]}
    )

    bot.post_reactive(CHANNEL, 120.0)

    # The cancel path returns before pick_rattle, and before any save.
    assert read_state()["used_indices"] == [0]


def test_post_reactive_ignores_a_cooldown_on_a_different_channel(
    bot, clock, write_state, slack_client
):
    write_state({"last_reactive": {}, "used_indices": []})
    clock.on_sleep = lambda c: write_state(
        {"last_reactive": {OTHER_CHANNEL: c.now()}, "used_indices": []}
    )

    bot.post_reactive(CHANNEL, 120.0)

    slack_client.chat_postMessage.assert_called_once_with(
        channel=CHANNEL, text="_first rattle_"
    )


def test_post_reactive_swallows_and_logs_a_post_failure(bot, slack_client, caplog):
    slack_client.chat_postMessage.side_effect = RuntimeError("slack is down")

    bot.post_reactive(CHANNEL, 1.0)

    assert len(errors(caplog)) == 1
    record = errors(caplog)[0]
    assert record.getMessage() == f"Error posting reactive rattle to {CHANNEL}"
    assert record.exc_info is not None


def test_post_reactive_failure_does_not_burn_the_cooldown(
    bot, fresh_state, slack_client, read_state
):
    slack_client.chat_postMessage.side_effect = RuntimeError("slack is down")

    bot.post_reactive(CHANNEL, 1.0)

    # The save happens *after* the post, so a failed post leaves the channel
    # free to try again immediately.
    assert CHANNEL not in read_state()["last_reactive"]


def test_post_reactive_failure_does_not_persist_the_consumed_index(
    bot, fresh_state, slack_client, read_state
):
    slack_client.chat_postMessage.side_effect = RuntimeError("slack is down")

    bot.post_reactive(CHANNEL, 1.0)

    # pick_rattle mutated the in-memory state, but nothing was written back.
    assert read_state()["used_indices"] == []


def test_post_reactive_swallows_a_state_load_failure(
    bot, state_path, slack_client, caplog
):
    state_path.write_text("{not json at all")

    bot.post_reactive(CHANNEL, 1.0)

    slack_client.chat_postMessage.assert_not_called()
    assert len(errors(caplog)) == 1
    assert errors(caplog)[0].exc_info is not None


def test_post_reactive_swallows_an_empty_rattle_list(make_bot, slack_client, caplog):
    bot = make_bot(rattles=[])

    bot.post_reactive(CHANNEL, 1.0)

    # random.choice on an empty sequence raises IndexError, which the blanket
    # `except Exception` eats.
    slack_client.chat_postMessage.assert_not_called()
    assert len(errors(caplog)) == 1
    assert errors(caplog)[0].exc_info is not None


def test_post_reactive_logs_a_save_failure_after_having_posted(
    make_bot, state_path, slack_client, caplog
):
    bot = make_bot(state_store=ExplodingSaveStore(state_path))

    bot.post_reactive(CHANNEL, 1.0)

    # The message really went out; only the bookkeeping failed. Slack has no
    # idea, and the cooldown is silently lost.
    slack_client.chat_postMessage.assert_called_once_with(
        channel=CHANNEL, text="_first rattle_"
    )
    assert len(errors(caplog)) == 1
    assert errors(caplog)[0].exc_info is not None


@pytest.mark.parametrize(
    "failure", ["post-raises", "load-raises", "save-raises", "no-rattles"]
)
def test_post_reactive_never_raises(
    failure, make_bot, slack_client, state_path, caplog
):
    overrides = {}
    if failure == "post-raises":
        slack_client.chat_postMessage.side_effect = RuntimeError("slack is down")
    elif failure == "load-raises":
        state_path.write_text("{not json at all")
    elif failure == "save-raises":
        overrides["state_store"] = ExplodingSaveStore(state_path)
    elif failure == "no-rattles":
        overrides["rattles"] = []
    bot = make_bot(**overrides)

    assert bot.post_reactive(CHANNEL, 1.0) is None
    assert len(errors(caplog)) == 1


def test_second_post_to_the_same_channel_is_cancelled_by_the_first(
    bot, slack_client, caplog
):
    bot.post_reactive(CHANNEL, 5.0)
    bot.post_reactive(CHANNEL, 5.0)

    assert slack_client.chat_postMessage.call_count == 1
    assert any("cancelled" in m for m in messages(caplog, logging.INFO))


# ------------------------------------------------------------ the real runner


def test_thread_task_runner_starts_one_daemon_thread(mocker):
    """The one test that touches the production runner.

    threading.Thread is patched, so no test in this suite ever starts a real
    thread -- which is what keeps every other test above deterministic.
    """
    thread_cls = mocker.patch("moelachbot.runner.threading.Thread")
    work = lambda: None  # noqa: E731

    ThreadTaskRunner().submit(work)

    thread_cls.assert_called_once_with(target=work, daemon=True)
    assert callable(thread_cls.call_args.kwargs["target"])
    assert thread_cls.call_args.kwargs["daemon"] is True
    thread_cls.return_value.start.assert_called_once_with()

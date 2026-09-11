"""Characterization tests for :meth:`moelachbot.core.MoelachBot.post_scheduled`.

``post_scheduled`` wraps its whole body in ``except Exception:
log.exception(...)``, so it can never raise: every failure mode is asserted
through ``caplog`` rather than ``pytest.raises``.

Several assertions below document behavior that is surprising rather than
desirable (notably the pin's off-by-one at ``"0"``). They are deliberate: this
suite exists to pin current behavior down for a behavior-preserving refactor.
"""

from __future__ import annotations

import dataclasses
import logging

import pytest

SCHEDULE_CHANNEL = "C_SCHED"  # matches the `config` fixture in conftest.py


# --------------------------------------------------------------------- helpers
# Local rather than in conftest.py: that file is being edited in parallel.


def bot_records(caplog):
    """Only records emitted by the bot's own loggers (caplog listens at root)."""
    return [r for r in caplog.records if r.name.startswith("moelachbot")]


def messages_at(caplog, level):
    return [r.getMessage() for r in bot_records(caplog) if r.levelno == level]


def error_records(caplog):
    return [r for r in bot_records(caplog) if r.levelno >= logging.ERROR]


def pinned_bot(make_bot, value, **overrides):
    return make_bot(config_overrides={"schedule_rattle": value}, **overrides)


# ==================================================== the random (unpinned) path


def test_posts_a_randomly_picked_rattle_to_the_schedule_channel(
    make_bot, slack_client
):
    bot = make_bot()

    bot.post_scheduled()

    # The stub RNG always takes the first available index -> rattles[0].
    slack_client.chat_postMessage.assert_called_once_with(
        channel=SCHEDULE_CHANNEL, text="_first rattle_"
    )


def test_random_pick_persists_the_used_index(make_bot, read_state):
    bot = make_bot()

    bot.post_scheduled()

    assert read_state()["used_indices"] == [0]


def test_message_is_italic_formatted(make_bot, slack_client, rattles):
    bot = make_bot()

    bot.post_scheduled()

    text = slack_client.chat_postMessage.call_args.kwargs["text"]
    assert text == f"_{rattles[0]}_"


def test_success_is_logged_naming_the_channel(make_bot, caplog):
    bot = make_bot()

    bot.post_scheduled()

    assert f"Posted scheduled rattle to {SCHEDULE_CHANNEL}" in messages_at(
        caplog, logging.INFO
    )


def test_empty_pin_falls_back_to_the_random_pick_path(
    make_bot, slack_client, read_state
):
    bot = pinned_bot(make_bot, "")

    bot.post_scheduled()

    slack_client.chat_postMessage.assert_called_once_with(
        channel=SCHEDULE_CHANNEL, text="_first rattle_"
    )
    assert read_state()["used_indices"] == [0]


# ============================================================== the pinned path


@pytest.mark.parametrize(
    ("pin", "expected"),
    [
        ("1", "first rattle"),
        ("2", "second rattle"),
        ("3", "third rattle"),
        (" 3 ", "third rattle"),  # int() tolerates surrounding whitespace
    ],
    ids=["pin-1", "pin-2", "pin-3", "pin-padded"],
)
def test_pinned_rattle_is_one_indexed(pin, expected, make_bot, slack_client):
    bot = pinned_bot(make_bot, pin)

    bot.post_scheduled()

    slack_client.chat_postMessage.assert_called_once_with(
        channel=SCHEDULE_CHANNEL, text=f"_{expected}_"
    )


def test_pin_zero_posts_the_last_rattle(make_bot, slack_client, rattles):
    bot = pinned_bot(make_bot, "0")

    bot.post_scheduled()

    # Deliberate documentation of an off-by-one: "0" - 1 == -1, which Python
    # reads as the LAST rattle rather than as an out-of-range error.
    slack_client.chat_postMessage.assert_called_once_with(
        channel=SCHEDULE_CHANNEL, text=f"_{rattles[-1]}_"
    )


def test_negative_pin_indexes_from_the_end(make_bot, slack_client, rattles):
    bot = pinned_bot(make_bot, "-1")

    bot.post_scheduled()

    # "-1" - 1 == -2 -> the second-to-last rattle. Also deliberate.
    slack_client.chat_postMessage.assert_called_once_with(
        channel=SCHEDULE_CHANNEL, text=f"_{rattles[-2]}_"
    )


def test_pinned_path_does_not_create_a_state_file(make_bot, state_path):
    bot = pinned_bot(make_bot, "2")

    bot.post_scheduled()

    # The pinned branch never loads or saves state, so nothing is written and
    # the pinned rattle is never marked as used.
    assert not state_path.exists()


def test_pinned_path_leaves_an_existing_state_file_untouched(
    make_bot, write_state, read_state
):
    write_state({"last_reactive": {"C_MON": 123.0}, "used_indices": [1]})
    bot = pinned_bot(make_bot, "2")

    bot.post_scheduled()

    assert read_state() == {"last_reactive": {"C_MON": 123.0}, "used_indices": [1]}


@pytest.mark.parametrize(
    "pin",
    ["999", "abc"],
    ids=["out-of-range-IndexError", "unparseable-ValueError"],
)
def test_bad_pin_posts_nothing_and_logs_an_error(
    pin, make_bot, slack_client, caplog
):
    bot = pinned_bot(make_bot, pin)

    bot.post_scheduled()

    # The exception is raised before chat.postMessage is reached, and is then
    # swallowed: the scheduled post is silently skipped for that week.
    slack_client.chat_postMessage.assert_not_called()
    errors = error_records(caplog)
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert (
        errors[0].getMessage()
        == f"Error posting scheduled rattle to {SCHEDULE_CHANNEL}"
    )


# ================================================================ failure modes


def test_post_failure_is_swallowed_and_logged(
    make_bot, slack_client, read_state, caplog
):
    slack_client.chat_postMessage.side_effect = RuntimeError("slack is down")
    bot = make_bot()

    bot.post_scheduled()

    errors = error_records(caplog)
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    # Deliberate: state is saved *before* the post, so a failed post still burns
    # the rattle — it will not come round again until the cycle resets.
    assert read_state()["used_indices"] == [0]
    assert f"Posted scheduled rattle to {SCHEDULE_CHANNEL}" not in messages_at(
        caplog, logging.INFO
    )


def test_corrupt_state_file_is_swallowed_and_nothing_is_posted(
    make_bot, state_path, slack_client, caplog
):
    state_path.write_text("{ not json at all")
    bot = make_bot()

    bot.post_scheduled()

    slack_client.chat_postMessage.assert_not_called()
    errors = error_records(caplog)
    assert len(errors) == 1
    assert errors[0].exc_info is not None


def test_empty_schedule_channel_still_calls_the_api(make_bot, slack_client):
    bot = make_bot(config_overrides={"schedule_channel": ""})

    bot.post_scheduled()

    # Nothing inside post_scheduled guards against an unset channel — that check
    # lives in main(). The call goes out with an empty channel regardless.
    slack_client.chat_postMessage.assert_called_once_with(
        channel="", text="_first rattle_"
    )


FAILURE_MODES = {
    "unparseable-pin": lambda bot, client, path: setattr(
        bot, "config", dataclasses.replace(bot.config, schedule_rattle="abc")
    ),
    "out-of-range-pin": lambda bot, client, path: setattr(
        bot, "config", dataclasses.replace(bot.config, schedule_rattle="999")
    ),
    "corrupt-state-file": lambda bot, client, path: path.write_text("{ nope"),
    "post-raises": lambda bot, client, path: setattr(
        client.chat_postMessage, "side_effect", RuntimeError("slack is down")
    ),
    "no-rattles-to-pick-from": lambda bot, client, path: setattr(bot, "rattles", []),
}


@pytest.mark.parametrize("mode", list(FAILURE_MODES), ids=list(FAILURE_MODES))
def test_post_scheduled_never_raises(
    mode, make_bot, slack_client, state_path, caplog
):
    bot = make_bot()
    FAILURE_MODES[mode](bot, slack_client, state_path)

    assert bot.post_scheduled() is None

    errors = error_records(caplog)
    assert len(errors) == 1
    assert errors[0].exc_info is not None

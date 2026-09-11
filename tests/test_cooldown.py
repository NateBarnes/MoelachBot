"""Tests for cooldown arithmetic.

Two surfaces are covered: the free function ``channel_on_cooldown``, which takes
``now`` and ``cooldown_hours`` explicitly, and ``MoelachBot.channel_on_cooldown``,
which pulls both from its injected clock and config.

The comparison is ``elapsed_hours < cooldown_hours`` -- strictly less-than -- so a
channel whose elapsed time exactly equals the cooldown is NOT on cooldown. The
boundary table below pins that down.
"""

from __future__ import annotations

import pytest

from moelachbot.state import channel_on_cooldown

NOW = 1_700_000_000.0
HOUR = 3600.0


def state_with(channel: str, elapsed_hours: float, now: float = NOW) -> dict:
    """State whose ``channel`` last posted ``elapsed_hours`` before ``now``."""
    return {"last_reactive": {channel: now - elapsed_hours * HOUR}}


# ------------------------------------------------------------- absent entries


def test_missing_last_reactive_key_is_not_on_cooldown():
    assert not channel_on_cooldown("C111", {}, now=NOW, cooldown_hours=48.0)


def test_state_with_only_used_indices_is_not_on_cooldown():
    state = {"used_indices": [0, 1, 2]}

    assert not channel_on_cooldown("C111", state, now=NOW, cooldown_hours=48.0)


def test_empty_last_reactive_is_not_on_cooldown():
    state = {"last_reactive": {}}

    assert not channel_on_cooldown("C111", state, now=NOW, cooldown_hours=48.0)


def test_channel_absent_from_last_reactive_is_not_on_cooldown():
    state = state_with("C_OTHER", elapsed_hours=0.0)

    assert not channel_on_cooldown("C111", state, now=NOW, cooldown_hours=48.0)


# ---------------------------------------------------------------- basic cases


def test_channel_that_just_posted_is_on_cooldown():
    state = state_with("C111", elapsed_hours=0.0)

    assert channel_on_cooldown("C111", state, now=NOW, cooldown_hours=48.0)


def test_channel_that_posted_long_ago_is_not_on_cooldown():
    state = state_with("C111", elapsed_hours=1000.0)

    assert not channel_on_cooldown("C111", state, now=NOW, cooldown_hours=48.0)


# -------------------------------------------------------------- boundary table


@pytest.mark.parametrize(
    ("elapsed_hours", "expected"),
    [
        pytest.param(0.0, True, id="just-posted"),
        pytest.param(47.99, True, id="just-under-threshold"),
        # Exactly at the threshold is NOT on cooldown: the comparison is `<`,
        # so elapsed == cooldown falls through. Documented current behavior.
        pytest.param(48.0, False, id="exactly-at-threshold"),
        pytest.param(48.01, False, id="just-over-threshold"),
        pytest.param(1000.0, False, id="ancient"),
        # A last_reactive timestamp in the future makes elapsed negative, and
        # negative is still < cooldown, so the channel reads as on cooldown.
        pytest.param(-1.0, True, id="future-timestamp"),
    ],
)
def test_cooldown_boundary(elapsed_hours, expected):
    state = state_with("C111", elapsed_hours)

    result = channel_on_cooldown("C111", state, now=NOW, cooldown_hours=48.0)

    assert result is expected


def test_future_timestamp_is_treated_as_on_cooldown():
    # Called out separately from the table because it is the surprising one: a
    # clock skew or a hand-edited state file pins the channel shut until the
    # timestamp is more than cooldown_hours in the past.
    state = {"last_reactive": {"C111": NOW + 10 * HOUR}}

    assert channel_on_cooldown("C111", state, now=NOW, cooldown_hours=48.0)


# ----------------------------------------------------------- cooldown_hours=0


@pytest.mark.parametrize("elapsed_hours", [0.0, 0.001, 1.0, 48.0, 1000.0])
def test_zero_cooldown_is_never_on_cooldown(elapsed_hours):
    state = state_with("C111", elapsed_hours)

    assert not channel_on_cooldown("C111", state, now=NOW, cooldown_hours=0.0)


# ------------------------------------------------------------ fractional hours


@pytest.mark.parametrize(
    ("elapsed_minutes", "expected"),
    [
        pytest.param(29, True, id="29-minutes-still-cooling"),
        pytest.param(31, False, id="31-minutes-free"),
    ],
)
def test_fractional_cooldown_of_half_an_hour(elapsed_minutes, expected):
    state = {"last_reactive": {"C111": NOW - elapsed_minutes * 60}}

    result = channel_on_cooldown("C111", state, now=NOW, cooldown_hours=0.5)

    assert result is expected


# --------------------------------------------------------------- per-channel


def test_cooldown_is_tracked_per_channel():
    state = {
        "last_reactive": {
            "C1": NOW,                 # just posted
            "C2": NOW - 100 * HOUR,    # long ago
        }
    }

    assert channel_on_cooldown("C1", state, now=NOW, cooldown_hours=48.0)
    assert not channel_on_cooldown("C2", state, now=NOW, cooldown_hours=48.0)


def test_hot_channel_does_not_affect_an_unlisted_channel():
    state = {"last_reactive": {"C1": NOW}}

    assert not channel_on_cooldown("C2", state, now=NOW, cooldown_hours=48.0)


def test_check_does_not_mutate_the_state_it_is_given():
    state = state_with("C111", elapsed_hours=1.0)
    before = {"last_reactive": dict(state["last_reactive"])}

    channel_on_cooldown("C111", state, now=NOW, cooldown_hours=48.0)
    channel_on_cooldown("C_MISSING", state, now=NOW, cooldown_hours=48.0)

    assert state == before


# ------------------------------------------------- MoelachBot.channel_on_cooldown


def test_bot_uses_the_injected_clock_and_advancing_it_clears_the_cooldown(
    make_bot, clock
):
    bot = make_bot()
    state = {"last_reactive": {"C_MON": clock.now()}}

    # True only because the bot reads the FakeClock: the fixture's start time is
    # far enough in the past that wall-clock now() would already be well beyond
    # the 48h cooldown.
    assert bot.channel_on_cooldown("C_MON", state) is True

    clock.advance(100 * HOUR)

    assert bot.channel_on_cooldown("C_MON", state) is False


def test_bot_reads_cooldown_hours_from_its_config(make_bot, clock):
    bot = make_bot(config_overrides={"cooldown_hours": 1.0})

    # Straddle the configured 1h: 0.5h inside, 1.5h outside. A doubled or
    # halved cooldown breaks one of these, whereas a single 2h-vs-1h check
    # lands exactly on the doubled boundary and passes either way.
    inside = {"last_reactive": {"C_MON": clock.now() - 0.5 * HOUR}}
    outside = {"last_reactive": {"C_MON": clock.now() - 1.5 * HOUR}}

    assert bot.channel_on_cooldown("C_MON", inside) is True
    assert bot.channel_on_cooldown("C_MON", outside) is False


def test_bot_returns_false_for_an_unknown_channel(make_bot):
    bot = make_bot()

    assert bot.channel_on_cooldown("C_NEVER_POSTED", {"last_reactive": {}}) is False


def test_bot_passes_its_configured_cooldown_through_unmodified(make_bot, clock):
    """Straddle the threshold so a scaled/garbled cooldown cannot slip past.

    Asserting the method equals a hand-rerun of the same call would be a mirror
    of the implementation: multiplying the configured hours by 2 inside the bot
    would satisfy both sides of that comparison.
    """
    bot = make_bot(config_overrides={"cooldown_hours": 10.0})

    just_inside = {"last_reactive": {"C_MON": clock.now() - 9.5 * HOUR}}
    just_outside = {"last_reactive": {"C_MON": clock.now() - 10.5 * HOUR}}

    assert bot.channel_on_cooldown("C_MON", just_inside) is True
    assert bot.channel_on_cooldown("C_MON", just_outside) is False


def test_bot_matches_the_free_function_on_a_straddling_pair(make_bot, clock):
    """Cross-check against the free function with values chosen so the two
    disagree if the bot scales, offsets or drops the configured cooldown."""
    bot = make_bot(config_overrides={"cooldown_hours": 10.0})

    for elapsed, expected in ((9.5, True), (10.5, False)):
        state = {"last_reactive": {"C_MON": clock.now() - elapsed * HOUR}}
        assert bot.channel_on_cooldown("C_MON", state) is expected
        assert (
            channel_on_cooldown(
                "C_MON", state, now=clock.now(), cooldown_hours=10.0
            )
            is expected
        )

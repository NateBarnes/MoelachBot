"""SystemClock is thin, but it is what main() injects in production, and a
wrong delegation here is invisible everywhere else in the suite (every other
test uses FakeClock)."""

from __future__ import annotations

import time

from moelachbot.clock import SystemClock


def test_now_tracks_the_wall_clock():
    clock = SystemClock()

    before = time.time()
    value = clock.now()
    after = time.time()

    assert isinstance(value, float)
    assert before <= value <= after


def test_sleep_delegates_to_time_sleep(mocker):
    # Patched rather than actually slept: the suite must stay fast and must
    # never depend on real timing.
    sleep = mocker.patch("moelachbot.clock.time.sleep")
    SystemClock().sleep(12.5)

    sleep.assert_called_once_with(12.5)


def test_ctime_formats_the_given_timestamp():
    clock = SystemClock()

    # Compared against time.ctime rather than a literal, so the assertion is
    # timezone-independent while still pinning the delegation.
    assert clock.ctime(1_700_000_000.0) == time.ctime(1_700_000_000.0)


def test_ctime_uses_its_argument_not_the_current_time():
    clock = SystemClock()

    assert clock.ctime(0.0) != clock.ctime(1_700_000_000.0)

"""Tests for ``Config.from_env``.

``from_env`` is a pure function: a mapping goes in, a frozen dataclass comes
out. Every test here therefore passes an explicit dict and never touches
``os.environ`` -- except for one test that proves the explicit dict wins.

Several assertions below document behavior that is surprising but deliberate
(empty tokens are accepted, a set-but-empty COOLDOWN_HOURS raises). Those are
marked inline so a future reader does not "fix" the test.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from moelachbot.config import Config, Paths

# ------------------------------------------------------------------ defaults

DEFAULTS = {
    "monitored_channels": (),
    "cooldown_hours": 48.0,
    "random_delay_max_minutes": 180,
    "schedule_channel": "",
    "schedule_day": "wed",
    "schedule_hour": 10,
    "schedule_minute": 0,
    "schedule_timezone": "America/Los_Angeles",
    "schedule_rattle": "",
}


def test_minimal_env_applies_all_defaults(env):
    cfg = Config.from_env(env())

    assert cfg.slack_bot_token == "xoxb-test"
    assert cfg.slack_app_token == "xapp-test"
    assert {f: getattr(cfg, f) for f in DEFAULTS} == DEFAULTS


@pytest.mark.parametrize(("field", "expected"), sorted(DEFAULTS.items()))
def test_each_default_individually(env, field, expected):
    assert getattr(Config.from_env(env()), field) == expected


def test_default_types(env):
    cfg = Config.from_env(env())

    assert isinstance(cfg.monitored_channels, tuple)
    assert isinstance(cfg.cooldown_hours, float)
    assert isinstance(cfg.random_delay_max_minutes, int)
    assert isinstance(cfg.schedule_hour, int)
    assert isinstance(cfg.schedule_minute, int)
    assert isinstance(cfg.schedule_rattle, str)


# ----------------------------------------------------------------- overrides


def test_all_eleven_settings_overridden(env):
    cfg = Config.from_env(
        {
            "SLACK_BOT_TOKEN": "xoxb-real",
            "SLACK_APP_TOKEN": "xapp-real",
            "MONITORED_CHANNELS": "C111,C222",
            "COOLDOWN_HOURS": "12.5",
            "RANDOM_DELAY_MAX_MINUTES": "30",
            "SCHEDULE_CHANNEL": "C_SCHED",
            "SCHEDULE_DAY": "fri",
            "SCHEDULE_HOUR": "23",
            "SCHEDULE_MINUTE": "45",
            "SCHEDULE_TIMEZONE": "Europe/London",
            "SCHEDULE_RATTLE": "7",
        }
    )

    assert cfg == Config(
        slack_bot_token="xoxb-real",
        slack_app_token="xapp-real",
        monitored_channels=("C111", "C222"),
        cooldown_hours=12.5,
        random_delay_max_minutes=30,
        schedule_channel="C_SCHED",
        schedule_day="fri",
        schedule_hour=23,
        schedule_minute=45,
        schedule_timezone="Europe/London",
        schedule_rattle="7",
    )


def test_overridden_values_have_the_right_types(env):
    cfg = Config.from_env(
        env(
            MONITORED_CHANNELS="C111",
            COOLDOWN_HOURS="12.5",
            RANDOM_DELAY_MAX_MINUTES="30",
            SCHEDULE_HOUR="23",
            SCHEDULE_MINUTE="45",
            SCHEDULE_RATTLE="7",
        )
    )

    assert type(cfg.monitored_channels) is tuple
    assert type(cfg.cooldown_hours) is float
    assert type(cfg.random_delay_max_minutes) is int
    assert type(cfg.schedule_hour) is int
    assert type(cfg.schedule_minute) is int
    assert type(cfg.schedule_rattle) is str


# -------------------------------------------------------------------- tokens


@pytest.mark.parametrize("missing", ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"])
def test_missing_token_raises_keyerror(env, missing):
    mapping = env()
    del mapping[missing]

    with pytest.raises(KeyError, match=missing):
        Config.from_env(mapping)


@pytest.mark.parametrize("token_key", ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"])
def test_empty_string_token_is_accepted(env, token_key):
    # Current behavior, documented on purpose: only a *missing* key raises.
    # A key that is present but empty sails straight through to the dataclass,
    # and the bot fails later at Slack-connect time instead of at startup.
    cfg = Config.from_env(env(**{token_key: ""}))

    assert getattr(cfg, token_key.lower()) == ""


# --------------------------------------------------------- MONITORED_CHANNELS


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("C111,C222", ("C111", "C222"), id="comma-split"),
        pytest.param(" C111 , C222 ", ("C111", "C222"), id="whitespace-stripped"),
        # "" must become () and NOT ("",) -- "".split(",") is [""], so the
        # `if c.strip()` filter is what saves us here.
        pytest.param("", (), id="empty-string-is-empty-tuple"),
        pytest.param("   ", (), id="whitespace-only-is-empty-tuple"),
        pytest.param("a,,b,", ("a", "b"), id="empty-segments-dropped"),
        pytest.param("C111", ("C111",), id="single-value"),
        pytest.param("C222,C111", ("C222", "C111"), id="order-preserved"),
        pytest.param("C111,C111", ("C111", "C111"), id="duplicates-preserved"),
        pytest.param(",", (), id="only-separators"),
    ],
)
def test_monitored_channels_parsing(env, raw, expected):
    assert Config.from_env(env(MONITORED_CHANNELS=raw)).monitored_channels == expected


def test_monitored_channels_absent_key_is_empty_tuple(env):
    mapping = env()
    assert "MONITORED_CHANNELS" not in mapping

    assert Config.from_env(mapping).monitored_channels == ()


def test_monitored_channels_is_a_tuple_not_a_list(env):
    # The dataclass is frozen, so the field is a tuple; assert against tuples.
    channels = Config.from_env(env(MONITORED_CHANNELS="C111,C222")).monitored_channels

    assert isinstance(channels, tuple)
    assert channels != ["C111", "C222"]


# --------------------------------------------------------------- numeric envs


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("48", 48.0), ("0", 0.0), ("1.5", 1.5), ("0.25", 0.25), (" 12 ", 12.0)],
)
def test_cooldown_hours_parsed_as_float(env, raw, expected):
    cfg = Config.from_env(env(COOLDOWN_HOURS=raw))

    assert cfg.cooldown_hours == expected
    assert isinstance(cfg.cooldown_hours, float)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("abc", id="non-numeric"),
        # Documented on purpose: COOLDOWN_HOURS="" raises rather than falling
        # back to 48. This is exactly why from_env uses env.get(k, default)
        # instead of `env.get(k) or default` -- the `or` form would silently
        # swallow an empty value and the failure mode would move to runtime.
        pytest.param("", id="set-but-empty"),
    ],
)
def test_cooldown_hours_invalid_raises_valueerror(env, raw):
    with pytest.raises(ValueError):
        Config.from_env(env(COOLDOWN_HOURS=raw))


def test_random_delay_max_minutes_parsed_as_int(env):
    cfg = Config.from_env(env(RANDOM_DELAY_MAX_MINUTES="5"))

    assert cfg.random_delay_max_minutes == 5
    assert isinstance(cfg.random_delay_max_minutes, int)


@pytest.mark.parametrize(
    "raw",
    [
        # int("1.5") raises: a float-looking delay is a hard startup failure,
        # not a truncation. Documented current behavior.
        pytest.param("1.5", id="float-string"),
        pytest.param("", id="set-but-empty"),
        pytest.param("lots", id="non-numeric"),
    ],
)
def test_random_delay_max_minutes_invalid_raises_valueerror(env, raw):
    with pytest.raises(ValueError):
        Config.from_env(env(RANDOM_DELAY_MAX_MINUTES=raw))


def test_schedule_hour_and_minute_parsed_as_int(env):
    cfg = Config.from_env(env(SCHEDULE_HOUR="7", SCHEDULE_MINUTE="30"))

    assert (cfg.schedule_hour, cfg.schedule_minute) == (7, 30)
    assert isinstance(cfg.schedule_hour, int)
    assert isinstance(cfg.schedule_minute, int)


@pytest.mark.parametrize("key", ["SCHEDULE_HOUR", "SCHEDULE_MINUTE"])
def test_schedule_time_non_numeric_raises_valueerror(env, key):
    with pytest.raises(ValueError):
        Config.from_env(env(**{key: "noon"}))


# ----------------------------------------------------------- schedule_rattle


@pytest.mark.parametrize("raw", ["7", "0", "", "not-a-number"])
def test_schedule_rattle_is_kept_as_a_string(env, raw):
    # Parsing is deliberately deferred to post time, where int() failures are
    # swallowed and logged rather than crashing startup. So "7" stays "7" and
    # even "not-a-number" is accepted here.
    cfg = Config.from_env(env(SCHEDULE_RATTLE=raw))

    assert cfg.schedule_rattle == raw
    assert isinstance(cfg.schedule_rattle, str)


# ------------------------------------------------------------ frozen / purity


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("slack_bot_token", "xoxb-other"),
        ("monitored_channels", ("C999",)),
        ("cooldown_hours", 1.0),
        ("schedule_rattle", "3"),
    ],
)
def test_config_is_frozen(env, field, value):
    cfg = Config.from_env(env())

    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(cfg, field, value)


def test_unrelated_env_keys_are_ignored(env):
    plain = Config.from_env(env())
    noisy = Config.from_env(
        env(PATH="/usr/bin", HOME="/root", MOELACH_FUTURE_SETTING="whatever")
    )

    assert noisy == plain


def test_from_env_with_explicit_mapping_ignores_os_environ(env, monkeypatch):
    # Purity check: os.environ is only consulted when env is None.
    monkeypatch.setenv("MONITORED_CHANNELS", "C_FROM_OS_ENVIRON")
    monkeypatch.setenv("COOLDOWN_HOURS", "999")
    monkeypatch.setenv("SCHEDULE_DAY", "mon")

    cfg = Config.from_env(env(MONITORED_CHANNELS="C_FROM_DICT"))

    assert cfg.monitored_channels == ("C_FROM_DICT",)
    assert cfg.cooldown_hours == 48.0
    assert cfg.schedule_day == "wed"


def test_from_env_does_not_mutate_the_mapping_it_is_given(env):
    mapping = env(MONITORED_CHANNELS="C111")
    before = dict(mapping)

    Config.from_env(mapping)

    assert mapping == before


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def test_paths_from_base_derives_all_three_files(tmp_path):
    """The three filenames are a compatibility contract with the pre-package
    layout, which anchored each on the repo-root bot.py's directory."""
    paths = Paths.from_base(tmp_path)

    assert paths.rattles == tmp_path / "death_rattles.md"
    assert paths.state == tmp_path / "state.json"
    assert paths.log == tmp_path / "moelachbot.log"


def test_paths_from_base_does_not_resolve_the_base(tmp_path):
    """No .resolve(): the original used Path(__file__).parent as-is, so a
    symlinked checkout must keep resolving to the symlink's directory."""
    relative = Path("some/relative/dir")
    paths = Paths.from_base(relative)

    assert paths.state == relative / "state.json"
    assert not paths.state.is_absolute()


def test_paths_is_frozen(tmp_path):
    paths = Paths.from_base(tmp_path)

    with pytest.raises(dataclasses.FrozenInstanceError):
        paths.state = tmp_path / "elsewhere.json"


# ---------------------------------------------------------------------------
# The os.environ fallback
#
# `env = os.environ if env is None else env` is a conditional EXPRESSION, which
# coverage.py does not record as a branch - so config.py reports 100% while
# this path has never executed.
# ---------------------------------------------------------------------------


def test_from_env_defaults_to_os_environ(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-from-environ")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-from-environ")
    monkeypatch.setenv("COOLDOWN_HOURS", "3.5")
    monkeypatch.setenv("MONITORED_CHANNELS", "C_ENV1,C_ENV2")

    config = Config.from_env()

    assert config.slack_bot_token == "xoxb-from-environ"
    assert config.slack_app_token == "xapp-from-environ"
    assert config.cooldown_hours == 3.5
    assert config.monitored_channels == ("C_ENV1", "C_ENV2")


def test_from_env_with_no_argument_still_requires_the_tokens(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)

    with pytest.raises(KeyError, match="SLACK_BOT_TOKEN"):
        Config.from_env()

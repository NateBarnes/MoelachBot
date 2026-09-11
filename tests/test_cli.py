"""Tests for the command-line surface: ``reset_cooldown``, ``run_bot`` wiring
and ``main``'s argument dispatch.

``main`` takes every heavyweight collaborator as a factory parameter, so these
tests build a real Config from a real dict, point ``base_dir`` at a tmp
directory, and hand in MagicMock factories -- no Bolt App, no APScheduler
thread, no socket-mode connection is ever constructed.

``reset_cooldown`` prints rather than logs: that output is the CLI's contract
with whoever is watching the terminal, so it is asserted through capsys.
"""

from __future__ import annotations

import io
import logging
import sys
from pathlib import Path

import pytest

from moelachbot.cli import main, reset_cooldown, setup_logging
from moelachbot.config import Config
from moelachbot.core import MoelachBot
from moelachbot.clock import SystemClock
from moelachbot.runner import ThreadTaskRunner

# ------------------------------------------------------------------ fixtures


@pytest.fixture(autouse=True)
def restore_bot_logger():
    """``setup_logging`` mutates the process-wide "moelachbot" logger and is
    idempotent, so without this the first test to call main() would own the log
    handlers (and a stale tmp file handle) for the rest of the session."""
    log = logging.getLogger("moelachbot")
    before, before_level = list(log.handlers), log.level
    yield
    for handler in log.handlers:
        if handler not in before:
            handler.close()
    log.handlers[:] = before
    log.setLevel(before_level)


@pytest.fixture
def base_dir(tmp_path):
    """Paths.from_base derives death_rattles.md / state.json / moelachbot.log
    from this, and it is the same tmp_path the state fixtures use."""
    (tmp_path / "death_rattles.md").write_text(
        '1. "first rattle"\n2. "second rattle"\n3. "third rattle"\n'
    )
    return tmp_path


class _FakeApp:
    """Stands in for the Bolt App: a client and an @app.event decorator.

    Deliberately a real class rather than a MagicMock. A MagicMock would
    auto-create `app.chat_postMessage`, so passing the App where a WebClient
    belongs (`client=app` instead of `client=app.client`) would look fine in
    tests and crash on every post in production.
    """

    def __init__(self, client):
        self.client = client
        self.registered: dict[str, object] = {}

    def event(self, name):
        def _decorator(fn):
            self.registered[name] = fn
            return fn

        return _decorator


@pytest.fixture
def fake_app(slack_client):
    return _FakeApp(slack_client)


@pytest.fixture
def app_factory(mocker, fake_app):
    return mocker.MagicMock(name="app_factory", return_value=fake_app)


@pytest.fixture
def scheduler_factory(mocker):
    return mocker.MagicMock(name="scheduler_factory")


@pytest.fixture
def handler_factory(mocker):
    return mocker.MagicMock(name="handler_factory")


@pytest.fixture
def built_bots(mocker):
    """Capture the MoelachBot run_bot builds, with its real behavior intact."""
    built: list[MoelachBot] = []

    def _build(**kwargs):
        bot = MoelachBot(**kwargs)
        built.append(bot)
        return bot

    mocker.patch("moelachbot.cli.MoelachBot", side_effect=_build)
    return built


@pytest.fixture
def run_main(base_dir, env, clock, app_factory, scheduler_factory, handler_factory):
    """run_main(["--send", "hi"], SCHEDULE_CHANNEL="C_SCHED")"""

    def _run(argv, **env_overrides):
        return main(
            argv,
            base_dir=base_dir,
            env=env(**env_overrides),
            clock=clock,
            app_factory=app_factory,
            scheduler_factory=scheduler_factory,
            handler_factory=handler_factory,
        )

    return _run


# ------------------------------------------------------------ reset_cooldown


def test_reset_named_channel_removes_it_and_says_so(store, write_state, clock, capsys):
    write_state({"last_reactive": {"C1": 1.0, "C2": 2.0}, "used_indices": [1]})

    reset_cooldown(store, "C1", clock)

    assert capsys.readouterr().out == "Cooldown cleared for channel: C1\n"


def test_reset_named_channel_persists_and_leaves_others_alone(
    store, write_state, read_state, clock
):
    write_state({"last_reactive": {"C1": 1.0, "C2": 2.0}, "used_indices": [1]})

    reset_cooldown(store, "C1", clock, out=io.StringIO())

    assert read_state() == {"last_reactive": {"C2": 2.0}, "used_indices": [1]}


def test_unknown_channel_lists_the_existing_cooldowns(store, write_state, clock):
    stamp = 1_700_000_000.0
    write_state({"last_reactive": {"C1": stamp}, "used_indices": []})
    out = io.StringIO()

    reset_cooldown(store, "C_NOPE", clock, out=out)

    assert out.getvalue().splitlines() == [
        "No cooldown found for channel: C_NOPE",
        "Current cooldowns:",
        # Formatted through the injected clock, never a hardcoded date string.
        f"  C1: {clock.ctime(stamp)}",
    ]


def test_unknown_channel_with_no_cooldowns_says_so(store, fresh_state, clock):
    out = io.StringIO()

    reset_cooldown(store, "C_NOPE", clock, out=out)

    assert out.getvalue().splitlines() == [
        "No cooldown found for channel: C_NOPE",
        "No cooldowns are currently set.",
    ]


def test_unknown_channel_still_saves_the_unchanged_state(
    store, write_state, read_state, clock, mocker
):
    original = {"last_reactive": {"C1": 1.0}, "used_indices": [2]}
    write_state(original)
    save = mocker.spy(store, "save")

    reset_cooldown(store, "C_NOPE", clock, out=io.StringIO())

    save.assert_called_once()
    assert read_state() == original


def test_reset_all_clears_every_channel(store, write_state, read_state, clock, capsys):
    write_state({"last_reactive": {"C1": 1.0, "C2": 2.0}, "used_indices": []})

    reset_cooldown(store, None, clock)

    assert capsys.readouterr().out == "All cooldowns cleared.\n"
    assert read_state()["last_reactive"] == {}


def test_reset_all_preserves_used_indices(store, write_state, read_state, clock):
    write_state({"last_reactive": {"C1": 1.0}, "used_indices": [0, 3, 7]})

    reset_cooldown(store, None, clock, out=io.StringIO())

    # Only last_reactive is touched; the rattle cycle survives a cooldown reset.
    assert read_state()["used_indices"] == [0, 3, 7]


def test_reset_all_on_a_missing_state_file_creates_it(
    store, state_path, read_state, clock
):
    assert not state_path.exists()

    reset_cooldown(store, None, clock, out=io.StringIO())

    assert state_path.exists()
    assert read_state() == {"last_reactive": {}, "used_indices": []}


def test_reset_cooldown_output_goes_to_stdout_not_the_log(
    store, fresh_state, clock, capsys, caplog
):
    reset_cooldown(store, None, clock)

    assert capsys.readouterr().out == "All cooldowns cleared.\n"
    assert caplog.records == []


# -------------------------------------------------------- main: --reset-cooldown


def test_reset_cooldown_flag_resets_one_channel_and_returns(
    run_main, write_state, read_state, capsys, app_factory, handler_factory
):
    write_state({"last_reactive": {"C1": 1.0, "C2": 2.0}, "used_indices": []})

    run_main(["--reset-cooldown", "C1"])

    assert read_state()["last_reactive"] == {"C2": 2.0}
    assert capsys.readouterr().out == "Cooldown cleared for channel: C1\n"
    app_factory.assert_not_called()
    handler_factory.assert_not_called()


def test_reset_cooldown_flag_without_a_value_resets_everything(
    run_main, write_state, read_state, capsys, handler_factory
):
    write_state({"last_reactive": {"C1": 1.0, "C2": 2.0}, "used_indices": []})

    run_main(["--reset-cooldown"])

    assert read_state()["last_reactive"] == {}
    assert capsys.readouterr().out == "All cooldowns cleared.\n"
    handler_factory.assert_not_called()


def test_reset_cooldown_literal_all_sentinel_is_indistinguishable(
    run_main, write_state, read_state, capsys
):
    # Deliberate: "__all__" is argparse's `const` for the no-value form, so a
    # user who types `--reset-cooldown __all__` (or genuinely has a channel
    # named __all__) gets the reset-everything branch. Documented as-is.
    write_state({"last_reactive": {"C1": 1.0, "__all__": 2.0}, "used_indices": []})

    run_main(["--reset-cooldown", "__all__"])

    assert read_state()["last_reactive"] == {}
    assert capsys.readouterr().out == "All cooldowns cleared.\n"


# ------------------------------------------------------------------ main: --send


def test_send_posts_to_the_schedule_channel_and_returns(
    run_main, slack_client, capsys, handler_factory
):
    run_main(["--send", "hello"], SCHEDULE_CHANNEL="C_SCHED")

    slack_client.chat_postMessage.assert_called_once_with(
        channel="C_SCHED", text="_hello_"
    )
    assert capsys.readouterr().out == "Sent to C_SCHED: _hello_\n"
    handler_factory.assert_not_called()


def test_send_wraps_the_message_in_italics(run_main, slack_client):
    # Deliberate and undocumented in the README: --send goes through
    # format_rattle, so the text always arrives italicised.
    run_main(["--send", "plain words"], SCHEDULE_CHANNEL="C_SCHED")

    assert slack_client.chat_postMessage.call_args.kwargs["text"] == "_plain words_"


def test_send_channel_flag_overrides_the_schedule_channel(run_main, slack_client):
    run_main(["--send", "hi", "--channel", "C_OVERRIDE"], SCHEDULE_CHANNEL="C_SCHED")

    slack_client.chat_postMessage.assert_called_once_with(
        channel="C_OVERRIDE", text="_hi_"
    )


def test_send_falls_back_to_the_schedule_channel(run_main, slack_client):
    run_main(["--send", "hi"], SCHEDULE_CHANNEL="C_FROM_ENV")

    assert slack_client.chat_postMessage.call_args.kwargs["channel"] == "C_FROM_ENV"


def test_send_with_no_channel_anywhere_prints_an_error(
    run_main, slack_client, capsys
):
    result = run_main(["--send", "hi"])

    assert (
        capsys.readouterr().out
        == "Error: no channel specified. Set SCHEDULE_CHANNEL or use --channel.\n"
    )
    slack_client.chat_postMessage.assert_not_called()
    # Deliberate: main returns None, i.e. exit status 0, even though nothing
    # was sent. Preserved behavior -- do not "fix" this into a non-zero exit.
    assert result is None


def test_send_falls_back_to_the_raw_name_when_resolution_drops_it(
    run_main, slack_client, channel_list, capsys
):
    channel_list(slack_client, {"general": "C_GEN"})

    run_main(["--send", "hi", "--channel", "ghost-channel"])

    # resolve_channel_ids returns [] for an unknown name, so the raw value is
    # used and Slack gets to reject it.
    slack_client.chat_postMessage.assert_called_once_with(
        channel="ghost-channel", text="_hi_"
    )
    assert capsys.readouterr().out == "Sent to ghost-channel: _hi_\n"


def test_send_skips_the_scope_check_and_never_starts_the_handler(
    run_main, slack_client, handler_factory, scheduler_factory
):
    run_main(["--send", "hi"], SCHEDULE_CHANNEL="C_SCHED")

    slack_client.auth_test.assert_not_called()
    handler_factory.assert_not_called()
    scheduler_factory.assert_not_called()


# ------------------------------------------------------------- main: run_bot


def test_startup_order_is_scopes_then_resolve_then_start(
    run_main, mocker, handler_factory
):
    manager = mocker.MagicMock()
    manager.attach_mock(mocker.patch("moelachbot.cli.check_scopes"), "check_scopes")
    manager.attach_mock(
        mocker.patch(
            "moelachbot.cli.resolve_config_channels",
            side_effect=lambda config, client, log: config,
        ),
        "resolve_config_channels",
    )
    manager.attach_mock(handler_factory.return_value.start, "start")

    run_main([], MONITORED_CHANNELS="C_MON")

    assert [name for name, _a, _k in manager.mock_calls] == [
        "check_scopes",
        "resolve_config_channels",
        "start",
    ]


def test_monitored_channels_are_resolved_before_the_bot_sees_them(
    run_main, slack_client, channel_list, built_bots
):
    channel_list(slack_client, {"general": "C_GEN", "random": "C_RND"})

    run_main([], MONITORED_CHANNELS="general,random")

    assert built_bots[0].config.monitored_channels == ("C_GEN", "C_RND")


def test_schedule_channel_is_resolved(run_main, slack_client, channel_list, built_bots):
    channel_list(slack_client, {"general": "C_GEN"})

    run_main([], SCHEDULE_CHANNEL="general")

    assert built_bots[0].config.schedule_channel == "C_GEN"


def test_unresolvable_schedule_channel_falls_back_to_the_raw_value(
    run_main, slack_client, channel_list, built_bots
):
    channel_list(slack_client, {"general": "C_GEN"})

    run_main([], SCHEDULE_CHANNEL="ghost-channel")

    assert built_bots[0].config.schedule_channel == "ghost-channel"


def test_no_scheduler_when_no_schedule_channel(run_main, scheduler_factory):
    run_main([], MONITORED_CHANNELS="C_MON")

    scheduler_factory.assert_not_called()


def test_scheduler_started_when_a_schedule_channel_is_set(run_main, scheduler_factory):
    run_main([], SCHEDULE_CHANNEL="C_SCHED")

    scheduler_factory.assert_called_once_with()
    scheduler_factory.return_value.start.assert_called_once_with()


def test_cron_trigger_is_built_from_the_configured_schedule(run_main, mocker):
    cron = mocker.patch("moelachbot.cli.CronTrigger")

    run_main(
        [],
        SCHEDULE_CHANNEL="C_SCHED",
        SCHEDULE_DAY="fri",
        SCHEDULE_HOUR=23,
        SCHEDULE_MINUTE=45,
        SCHEDULE_TIMEZONE="Europe/London",
    )

    # Construction only: firing semantics belong to APScheduler, not to us.
    cron.assert_called_once_with(
        day_of_week="fri", hour=23, minute=45, timezone="Europe/London"
    )


def test_scheduled_job_is_the_bots_bound_post_scheduled(
    run_main, mocker, scheduler_factory, built_bots
):
    cron = mocker.patch("moelachbot.cli.CronTrigger")

    run_main([], SCHEDULE_CHANNEL="C_SCHED")

    scheduler_factory.return_value.add_job.assert_called_once_with(
        built_bots[0].post_scheduled, cron.return_value
    )


def test_handler_is_built_with_the_app_token_and_started(
    run_main, handler_factory, fake_app
):
    run_main([], MONITORED_CHANNELS="C_MON")

    handler_factory.assert_called_once_with(fake_app, "xapp-test")
    handler_factory.return_value.start.assert_called_once_with()


def test_app_factory_receives_the_config(run_main, app_factory):
    run_main([], MONITORED_CHANNELS="C_MON")

    (config,), _kwargs = app_factory.call_args
    assert isinstance(config, Config)
    assert config.slack_bot_token == "xoxb-test"


def test_startup_banner_is_logged(run_main, caplog):
    run_main(
        [],
        MONITORED_CHANNELS="C_MON",
        SCHEDULE_CHANNEL="C_SCHED",
        COOLDOWN_HOURS="12.5",
        RANDOM_DELAY_MAX_MINUTES="30",
        SCHEDULE_RATTLE="7",
    )

    logged = [r.getMessage() for r in caplog.records]
    assert "Starting MoelachBot" in logged
    # list(...) so the banner reads ['C_MON'], not ('C_MON',)
    assert "  Monitored channels: ['C_MON']" in logged
    assert "  Cooldown: 12.5 hours" in logged
    assert "  Random delay max: 30 minutes" in logged
    assert "  Schedule channel: C_SCHED" in logged
    assert "  Scheduled rattle: #7" in logged
    assert "MoelachBot is running" in logged


def test_banner_says_none_when_nothing_is_configured(run_main, caplog):
    run_main([])

    logged = [r.getMessage() for r in caplog.records]
    assert "  Monitored channels: (none)" in logged
    assert "  Schedule channel: (none)" in logged
    assert not any(m.startswith("  Scheduled rattle") for m in logged)


def test_argv_is_injected_not_read_from_sys_argv(
    run_main, monkeypatch, write_state, read_state, slack_client
):
    monkeypatch.setattr(sys, "argv", ["bot.py", "--send", "poison"])
    write_state({"last_reactive": {"C1": 1.0}, "used_indices": []})

    run_main(["--reset-cooldown", "C1"])

    assert read_state()["last_reactive"] == {}
    slack_client.chat_postMessage.assert_not_called()


def test_message_handler_is_registered_and_delegates_to_the_bot(
    run_main, fake_app, built_bots, mocker
):
    run_main([], MONITORED_CHANNELS="C_MON")

    assert list(fake_app.registered) == ["message"]
    handle = mocker.patch.object(built_bots[0], "handle_message")
    event = {"channel": "C_MON", "text": "hi"}

    # Bolt injects by parameter name, hence the unused `say`.
    fake_app.registered["message"](event=event, say=mocker.MagicMock())

    handle.assert_called_once_with(event)


# ---------------------------------------------------------------------------
# Dependency wiring
#
# run_bot hands MoelachBot eight collaborators. Asserting only on the resulting
# behavior leaves every one of those arguments free to be wrong: passing `app`
# where `app.client` belongs, or a runner that executes inline instead of on
# a thread, breaks production without breaking a behavioral test.
# ---------------------------------------------------------------------------


def test_run_bot_wires_the_production_collaborators(
    run_main, built_bots, slack_client, base_dir, clock
):
    run_main([], MONITORED_CHANNELS="C_MON")

    assert len(built_bots) == 1
    bot = built_bots[0]

    # The WebClient, NOT the Bolt App: app has no chat_postMessage.
    assert bot.client is slack_client
    # A real thread, or a reactive post blocks the Bolt listener for its delay.
    assert isinstance(bot.runner, ThreadTaskRunner)
    # Rattles come from base_dir/death_rattles.md, not an empty list.
    assert bot.rattles == ["first rattle", "second rattle", "third rattle"]
    # State goes to state.json, not the log file.
    assert bot.state_store.path == base_dir / "state.json"
    assert bot.clock is clock
    assert bot.log.name == "moelachbot"


def test_run_bot_uses_a_real_random_instance(run_main, built_bots):
    import random

    run_main([], MONITORED_CHANNELS="C_MON")
    assert isinstance(built_bots[0].rng, random.Random)


def test_main_defaults_to_a_system_clock(
    base_dir, env, app_factory, scheduler_factory, handler_factory, built_bots
):
    # clock is omitted entirely, so main() must supply the production one.
    main(
        [],
        base_dir=base_dir,
        env=env(MONITORED_CHANNELS="C_MON"),
        app_factory=app_factory,
        scheduler_factory=scheduler_factory,
        handler_factory=handler_factory,
    )
    assert isinstance(built_bots[0].clock, SystemClock)


# ---------------------------------------------------------------------------
# setup_logging
#
# Previously untested entirely: the logger name, both handler levels, the
# formatter, the file path and the idempotency guard were all free to change.
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_logger_name(request):
    """A unique logger per test, torn down so the global registry stays clean."""
    name = f"moelachbot.test_setup.{request.node.name}"
    yield name
    log = logging.getLogger(name)
    for h in list(log.handlers):
        h.close()
        log.removeHandler(h)
    log.setLevel(logging.NOTSET)


def test_setup_logging_configures_console_and_file_at_debug(tmp_path, temp_logger_name):
    log = setup_logging(tmp_path / "bot.log", name=temp_logger_name)

    assert log.name == temp_logger_name
    assert log.level == logging.DEBUG
    assert len(log.handlers) == 2
    assert {h.level for h in log.handlers} == {logging.DEBUG}
    file_handlers = [h for h in log.handlers if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 1
    assert Path(file_handlers[0].baseFilename) == tmp_path / "bot.log"


def test_setup_logging_is_idempotent(tmp_path, temp_logger_name):
    first = setup_logging(tmp_path / "bot.log", name=temp_logger_name)
    count = len(first.handlers)
    second = setup_logging(tmp_path / "bot.log", name=temp_logger_name)

    assert second is first
    assert len(second.handlers) == count  # not doubled


def test_setup_logging_writes_timestamped_levelled_records(tmp_path, temp_logger_name):
    log_path = tmp_path / "bot.log"
    log = setup_logging(log_path, name=temp_logger_name)
    log.propagate = False  # keep this out of caplog's root capture

    log.warning("a distinctive line")
    for handler in log.handlers:
        handler.flush()

    written = log_path.read_text()
    assert "a distinctive line" in written
    assert "[WARNING]" in written
    # Leading timestamp from "%(asctime)s [%(levelname)s] %(message)s"
    assert written.startswith("20") or written[:4].isdigit()


def test_main_configures_the_real_logger_and_writes_the_log_file(run_main, base_dir):
    run_main([], MONITORED_CHANNELS="C_MON")

    assert (base_dir / "moelachbot.log").exists()
    assert (base_dir / "moelachbot.log").read_text().strip()


def test_send_resolves_a_channel_name_to_its_id(
    run_main, slack_client, channel_list, capsys
):
    """Every other --send test uses a C-prefixed value (passed through
    unresolved) or an unresolvable name (falls back to raw), so neither
    exercises the resolution branch that this line exists for."""
    channel_list(slack_client, {"general": "C_GEN"})

    run_main(["--send", "hi", "--channel", "general"])

    slack_client.chat_postMessage.assert_called_once_with(
        channel="C_GEN", text="_hi_"
    )
    assert "Sent to C_GEN: _hi_" in capsys.readouterr().out

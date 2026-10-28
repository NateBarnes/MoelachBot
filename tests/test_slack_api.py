"""Characterization tests for :mod:`moelachbot.slack_api`.

Every assertion documents what the code does TODAY — warts included — so that a
behavior-preserving refactor has something to fail against. Where the current
behavior is surprising, the test says so in a comment instead of asserting the
behavior we would rather have.

All three functions under test swallow failures via
``except Exception: log.exception(...)``, so error paths are asserted through
``caplog`` records and never through ``pytest.raises``.
"""

from __future__ import annotations

import dataclasses
import logging

import pytest

from moelachbot import slack_api
from moelachbot.config import Config
from moelachbot.slack_api import (
    REQUIRED_SCOPES,
    check_scopes,
    resolve_channel_ids,
    resolve_config_channels,
)

ALL_SCOPES = "channels:history,channels:read,chat:write"
BANNER = "=" * 60


# --------------------------------------------------------------------- helpers
# Local rather than in conftest.py: that file is being edited in parallel.


def bot_records(caplog):
    """Only records emitted by the bot's own loggers (caplog listens at root)."""
    return [r for r in caplog.records if r.name.startswith("moelachbot")]


def messages_at(caplog, level):
    return [r.getMessage() for r in bot_records(caplog) if r.levelno == level]


def error_records(caplog):
    return [r for r in bot_records(caplog) if r.levelno >= logging.ERROR]


# =============================================================== check_scopes


def test_all_scopes_present_logs_one_info_and_no_warnings(
    slack_client, auth_response, log, caplog
):
    slack_client.auth_test.return_value = auth_response(scopes=ALL_SCOPES)

    check_scopes(slack_client, log)

    assert messages_at(caplog, logging.INFO) == [
        "All required OAuth scopes are present: "
        "channels:history, channels:read, chat:write"
    ]
    assert messages_at(caplog, logging.WARNING) == []


def test_missing_scope_is_named_with_its_human_description(
    slack_client, auth_response, log, caplog
):
    slack_client.auth_test.return_value = auth_response(
        scopes="channels:history,channels:read"
    )

    check_scopes(slack_client, log)

    warnings = messages_at(caplog, logging.WARNING)
    assert warnings.count(BANNER) == 2  # opening AND closing rule
    assert "MISSING REQUIRED OAUTH SCOPES" in warnings
    assert any(
        "chat:write" in m and REQUIRED_SCOPES["chat:write"] in m for m in warnings
    )
    # Scopes that *are* granted are never mentioned in the banner.
    assert not any("channels:read" in m for m in warnings)


def test_all_scopes_missing_are_listed_in_sorted_order(
    slack_client, auth_response, log, caplog
):
    slack_client.auth_test.return_value = auth_response(scopes="users:read")

    check_scopes(slack_client, log)

    named = [m for m in messages_at(caplog, logging.WARNING) if m.startswith("  - ")]
    assert named == [
        "  - channels:history  (receive message events in public channels)",
        "  - channels:read  (resolve channel names to IDs)",
        "  - chat:write  (post messages)",
    ]


def test_missing_scopes_banner_includes_remediation_url(
    slack_client, auth_response, log, caplog
):
    # An *empty* header short-circuits before the banner, so a real-but-
    # insufficient scope string is needed to reach the remediation line.
    slack_client.auth_test.return_value = auth_response(scopes="users:read")

    check_scopes(slack_client, log)

    warnings = messages_at(caplog, logging.WARNING)
    assert any("https://api.slack.com/apps" in m for m in warnings)


def test_superset_of_required_scopes_produces_no_warnings(
    slack_client, auth_response, log, caplog
):
    slack_client.auth_test.return_value = auth_response(
        scopes=ALL_SCOPES + ",users:read,files:write"
    )

    check_scopes(slack_client, log)

    assert messages_at(caplog, logging.WARNING) == []
    # The INFO line reports everything granted, not just what was required.
    assert messages_at(caplog, logging.INFO) == [
        "All required OAuth scopes are present: channels:history, channels:read, "
        "chat:write, files:write, users:read"
    ]


def test_whitespace_around_commas_is_stripped(
    slack_client, auth_response, log, caplog
):
    slack_client.auth_test.return_value = auth_response(
        scopes="channels:history , channels:read,chat:write"
    )

    check_scopes(slack_client, log)

    assert messages_at(caplog, logging.WARNING) == []
    assert len(messages_at(caplog, logging.INFO)) == 1


def test_empty_scope_header_warns_and_returns_before_the_banner(
    slack_client, auth_response, log, caplog
):
    slack_client.auth_test.return_value = auth_response(scopes="")

    check_scopes(slack_client, log)

    warnings = messages_at(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert warnings[0].startswith("Could not determine bot scopes")
    # Deliberate: an empty header means "unknown", never "all scopes missing".
    assert BANNER not in warnings


def test_absent_scope_header_key_is_treated_as_empty(
    slack_client, auth_response, log, caplog
):
    slack_client.auth_test.return_value = auth_response(scopes=None)  # headers={}

    check_scopes(slack_client, log)

    warnings = messages_at(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert warnings[0].startswith("Could not determine bot scopes")


def test_response_without_headers_attribute_skips_validation(
    slack_client, headerless_response, log, caplog
):
    # Exercises the `hasattr(resp, "headers")` guard: no attribute at all.
    slack_client.auth_test.return_value = headerless_response()

    check_scopes(slack_client, log)

    warnings = messages_at(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert warnings[0].startswith("Could not determine bot scopes")
    assert BANNER not in warnings


def test_auth_test_failure_is_swallowed_and_logged(slack_client, log, caplog):
    slack_client.auth_test.side_effect = RuntimeError("slack is down")

    check_scopes(slack_client, log)  # deliberately no pytest.raises

    errors = error_records(caplog)
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert errors[0].getMessage() == "Failed to validate OAuth scopes"


def test_auth_test_is_called_exactly_once_with_no_arguments(slack_client, log):
    check_scopes(slack_client, log)

    slack_client.auth_test.assert_called_once_with()




def test_empty_name_list_returns_empty_and_makes_no_api_call(slack_client, log):
    assert resolve_channel_ids(slack_client, [], log) == []

    slack_client.conversations_list.assert_not_called()


def test_names_resolve_to_ids_with_one_info_line_each(
    slack_client, channel_list, log, caplog
):
    channel_list(slack_client, {"general": "C1", "random": "C2"})

    assert resolve_channel_ids(slack_client, ["general", "random"], log) == ["C1", "C2"]

    assert messages_at(caplog, logging.INFO) == [
        "Resolved channel 'general' -> C1",
        "Resolved channel 'random' -> C2",
    ]


def test_id_like_name_passes_through_but_api_is_still_called(
    slack_client, channel_list, log, caplog
):
    channel_list(slack_client, {"general": "C1"})

    assert resolve_channel_ids(slack_client, ["C0123ALREADY"], log) == ["C0123ALREADY"]

    # Deliberate documentation: the early return only covers an *empty* list, so
    # a list made entirely of IDs still pays for a conversations.list round trip.
    slack_client.conversations_list.assert_called_once()
    assert messages_at(caplog, logging.INFO) == []


def test_unknown_name_passes_through_with_an_info_line(
    slack_client, channel_list, log, caplog
):
    channel_list(slack_client, {"general": "C1"})
    names = ["general", "ghost"]

    resolved = resolve_channel_ids(slack_client, names, log)

    # One entry per input name: 'ghost' survives as itself so chat.postMessage
    # still gets something, and so a caller that needs a real ID can tell.
    assert resolved == ["C1", "ghost"]
    assert len(resolved) == len(names)
    # INFO, not WARNING: nothing is broken by a passthrough at this layer.
    assert messages_at(caplog, logging.WARNING) == []
    assert messages_at(caplog, logging.INFO) == [
        "Resolved channel 'general' -> C1",
        "Channel 'ghost' is not among the 1 channels this token can list "
        "— passing the name through to Slack, which accepts names",
    ]


def test_all_names_unknown_pass_through_unchanged(slack_client, channel_list, log):
    channel_list(slack_client, {"general": "C1"})
    names = ["ghost", "phantom"]

    # The length invariant is the whole contract, so it is asserted even here
    # where nothing resolves.
    assert resolve_channel_ids(slack_client, names, log) == names


def test_mixed_ids_known_and_unknown_preserve_input_order(
    slack_client, channel_list, log
):
    channel_list(slack_client, {"general": "C1", "random": "C2"})

    # Input is deliberately NOT in sorted order: with ["CRAW123", "general",
    # "ghost", "random"] ('C' < 'g' in ASCII) an implementation that sorted its
    # input would produce the same answer and the test would prove nothing.
    resolved = resolve_channel_ids(
        slack_client, ["random", "CRAW123", "ghost", "general"], log
    )

    assert resolved == ["C2", "CRAW123", "ghost", "C1"]


def test_conversations_list_called_with_exact_kwargs(
    slack_client, channel_list, log
):
    channel_list(slack_client, {"general": "C1"})

    resolve_channel_ids(slack_client, ["general"], log)

    # cursor is always passed, even on the first page: slack_sdk strips None
    # values from the payload, so the request on the wire is unchanged.
    slack_client.conversations_list.assert_called_once_with(
        types="public_channel", limit=1000, cursor=None
    )


def test_api_exception_returns_the_input_names_unchanged(
    slack_client, log, caplog
):
    slack_client.conversations_list.side_effect = RuntimeError("boom")
    names = ["general", "ghost"]

    resolved = resolve_channel_ids(slack_client, names, log)

    assert resolved == ["general", "ghost"]
    assert resolved is not names  # a copy via list(names), not the caller's object
    errors = error_records(caplog)
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert (
        errors[0].getMessage()
        == "Failed to resolve channel names, falling back to raw values"
    )


def test_missing_channels_key_falls_back_to_raw_names(slack_client, log, caplog):
    # A KeyError from result["channels"] lands in the same except branch as an
    # API failure — there is no separate malformed-response path.
    slack_client.conversations_list.return_value = {}

    assert resolve_channel_ids(slack_client, ["general"], log) == ["general"]

    assert len(error_records(caplog)) == 1


def test_channel_entry_missing_name_falls_back_to_raw_names(
    slack_client, log, caplog
):
    slack_client.conversations_list.return_value = {"channels": [{"id": "C1"}]}

    assert resolve_channel_ids(slack_client, ["general"], log) == ["general"]

    errors = error_records(caplog)
    assert len(errors) == 1
    assert errors[0].exc_info is not None


def test_id_detection_is_case_sensitive(slack_client, channel_list, log, caplog):
    channel_list(slack_client, {"general": "C1"})

    # Lowercase "cats" is NOT an ID: startswith("C") is case-sensitive, so it is
    # looked up, missed, and passed through with an INFO line...
    assert resolve_channel_ids(slack_client, ["cats"], log) == ["cats"]
    assert messages_at(caplog, logging.WARNING) == []
    assert any("'cats' is not among" in m for m in messages_at(caplog, logging.INFO))

    # ...while capital-C "Cats" is waved straight through as if it were an ID.
    assert resolve_channel_ids(slack_client, ["Cats"], log) == ["Cats"]


def test_duplicate_names_resolve_to_duplicate_ids(
    slack_client, channel_list, log, caplog
):
    channel_list(slack_client, {"general": "C1"})

    # Deliberate: there is no de-duplication anywhere in the pipeline.
    assert resolve_channel_ids(slack_client, ["general", "general"], log) == [
        "C1",
        "C1",
    ]
    assert len(messages_at(caplog, logging.INFO)) == 2


def test_length_invariant_holds_for_every_mix_of_inputs(
    slack_client, channel_list, log
):
    """The contract the rest of the module leans on. Dropping names is what
    used to hand resolve_config_channels an empty monitored tuple."""
    channel_list(slack_client, {"general": "C1"})
    names = ["general", "ghost", "C0RAW", "general"]

    assert len(resolve_channel_ids(slack_client, names, log)) == len(names)


def test_cursor_is_followed_across_pages(slack_client, channel_pages, log):
    channel_pages(slack_client, [{"general": "C1"}, {"on-page-two": "C2"}])

    resolved = resolve_channel_ids(slack_client, ["general", "on-page-two"], log)

    assert resolved == ["C1", "C2"]
    assert slack_client.conversations_list.call_count == 2
    assert slack_client.conversations_list.call_args_list[0].kwargs["cursor"] is None
    assert (
        slack_client.conversations_list.call_args_list[1].kwargs["cursor"]
        == "cursor-1"
    )


def test_an_empty_next_cursor_stops_after_one_page(
    slack_client, channel_pages, log
):
    channel_pages(slack_client, [{"general": "C1"}])

    assert resolve_channel_ids(slack_client, ["general"], log) == ["C1"]
    assert slack_client.conversations_list.call_count == 1


def test_null_response_metadata_is_treated_as_no_cursor(
    slack_client, log, caplog
):
    # Slack sends an explicit null here on some final pages; `.get(k, {})` would
    # hand back that None and the next .get would raise straight into the
    # except branch, turning a good response into "falling back to raw values".
    slack_client.conversations_list.return_value = {
        "channels": [{"name": "general", "id": "C1"}],
        "response_metadata": None,
    }

    assert resolve_channel_ids(slack_client, ["general"], log) == ["C1"]
    assert error_records(caplog) == []
    assert slack_client.conversations_list.call_count == 1


def test_a_repeating_cursor_stops_instead_of_looping(slack_client, log):
    # A plain return_value replays the same page, cursor included, forever.
    slack_client.conversations_list.return_value = {
        "channels": [{"name": "general", "id": "C1"}],
        "response_metadata": {"next_cursor": "stuck"},
    }

    assert resolve_channel_ids(slack_client, ["general"], log) == ["C1"]
    # Second call is the one that sees the cursor repeat and bails.
    assert slack_client.conversations_list.call_count == 2


def test_the_page_ceiling_warns_rather_than_truncating_silently(
    slack_client, log, caplog, mocker
):
    mocker.patch.object(slack_api, "MAX_CHANNEL_PAGES", 3)
    pages = [
        {
            "channels": [{"name": f"c{i}", "id": f"C{i}"}],
            "response_metadata": {"next_cursor": f"cursor-{i}"},
        }
        for i in range(3)
    ]
    slack_client.conversations_list.side_effect = pages

    assert resolve_channel_ids(slack_client, ["c0"], log) == ["C0"]
    assert slack_client.conversations_list.call_count == 3
    assert messages_at(caplog, logging.WARNING) == [
        "Stopped paging conversations.list at 3 pages; the channel list may be "
        "incomplete, so some names will not resolve"
    ]


# ===================================================== resolve_config_channels


def test_returns_a_new_frozen_config_and_leaves_the_original_unmutated(
    config, slack_client, channel_list, log
):
    channel_list(slack_client, {"general": "C1", "announce": "C2"})
    original = dataclasses.replace(
        config, monitored_channels=("general",), schedule_channel="announce"
    )

    result = resolve_config_channels(original, slack_client, log)

    assert isinstance(result, Config)
    assert result is not original
    assert original.monitored_channels == ("general",)
    assert original.schedule_channel == "announce"
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.schedule_channel = "C_OTHER"


def test_resolves_both_monitored_channels_and_schedule_channel(
    config, slack_client, channel_list, log
):
    channel_list(slack_client, {"general": "C1", "random": "C2", "announce": "C3"})
    original = dataclasses.replace(
        config, monitored_channels=("general", "random"), schedule_channel="announce"
    )

    result = resolve_config_channels(original, slack_client, log)

    assert result.monitored_channels == ("C1", "C2")
    assert isinstance(result.monitored_channels, tuple)
    assert result.schedule_channel == "C3"


def test_schedule_channel_keeps_its_raw_value_when_it_is_not_public(
    config, slack_client, channel_list, log, caplog
):
    channel_list(slack_client, {"general": "C1"})
    original = dataclasses.replace(
        config, monitored_channels=("general",), schedule_channel="ghost"
    )

    result = resolve_config_channels(original, slack_client, log)

    # The resolver passes the name through, so the raw value reaches the Config
    # and is later handed to chat.postMessage as-is. No warning: only the
    # *monitored* list is broken by a name that never became an ID.
    assert result.schedule_channel == "ghost"
    assert messages_at(caplog, logging.WARNING) == []
    assert any("'ghost' is not among" in m for m in messages_at(caplog, logging.INFO))


def test_empty_schedule_channel_skips_the_second_lookup(
    config, slack_client, channel_list, log
):
    channel_list(slack_client, {"general": "C1"})
    original = dataclasses.replace(
        config, monitored_channels=("general",), schedule_channel=""
    )

    result = resolve_config_channels(original, slack_client, log)

    assert result.schedule_channel == ""
    assert slack_client.conversations_list.call_count == 1


def test_no_channels_at_all_makes_no_api_calls(config, slack_client, log):
    original = dataclasses.replace(
        config, monitored_channels=(), schedule_channel=""
    )

    result = resolve_config_channels(original, slack_client, log)

    assert result.monitored_channels == ()
    assert result.schedule_channel == ""
    slack_client.conversations_list.assert_not_called()


def test_an_unresolved_monitored_channel_warns_with_both_requirements(
    config, slack_client, channel_list, log, caplog
):
    channel_list(slack_client, {"general": "C1"})
    original = dataclasses.replace(
        config, monitored_channels=("moelach_test",), schedule_channel=""
    )

    result = resolve_config_channels(original, slack_client, log)

    # The name survives, so the startup banner reports what is configured
    # instead of lying with "(none)".
    assert result.monitored_channels == ("moelach_test",)
    warnings = messages_at(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert "reactive posting will never fire" in warnings[0]
    # Both halves, or the reader adds groups:read and stays broken.
    assert "groups:read" in warnings[0]
    assert "groups:history" in warnings[0]
    assert "message.groups" in warnings[0]


def test_a_monitored_channel_given_as_an_id_does_not_warn(
    config, slack_client, channel_list, log, caplog
):
    """An ID is passed through unchanged too, so the input == output test for a
    passthrough has to exclude it or every correctly-configured bot warns."""
    channel_list(slack_client, {"general": "C1"})
    original = dataclasses.replace(
        config, monitored_channels=("C0ALREADY",), schedule_channel=""
    )

    result = resolve_config_channels(original, slack_client, log)

    assert result.monitored_channels == ("C0ALREADY",)
    assert messages_at(caplog, logging.WARNING) == []


def test_an_unresolved_schedule_channel_does_not_warn(
    config, slack_client, channel_list, log, caplog
):
    channel_list(slack_client, {"general": "C1"})
    original = dataclasses.replace(
        config, monitored_channels=(), schedule_channel="moelach_test"
    )

    result = resolve_config_channels(original, slack_client, log)

    # chat.postMessage takes the name, so there is nothing to warn about.
    assert result.schedule_channel == "moelach_test"
    assert messages_at(caplog, logging.WARNING) == []

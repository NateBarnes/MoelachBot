"""Characterization tests for :mod:`moelachbot.rattles`.

These tests pin down *current* behavior ahead of a behavior-preserving
refactor. Several of the parser's and picker's quirks are surprising; where
that is the case the assertion is deliberate and the comment says so. Nothing
here is a wish list — if a test looks like it is asserting a bug, it is
asserting a bug that production depends on today.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from moelachbot.rattles import format_rattle, load_death_rattles, pick_rattle

# --------------------------------------------------------------------- helpers


def _indices(rattles: list[str], picked: list[str]) -> list[int]:
    """Map picked texts back to their positions (sample texts are unique)."""
    return [rattles.index(text) for text in picked]


class ScriptedRandom:
    """Local RNG stub that returns a pre-set index (conftest's StubRandom
    always takes seq[0] unless its private script is loaded)."""

    def __init__(self, *choices: int) -> None:
        self._choices = list(choices)
        self.choice_calls: list[list[Any]] = []

    def choice(self, seq):
        self.choice_calls.append(list(seq))
        want = self._choices.pop(0)
        assert want in seq, f"scripted choice {want!r} not in {list(seq)!r}"
        return want


# ------------------------------------------------------- load_death_rattles


def test_parses_numbered_entries(rattles_file):
    path = rattles_file(
        '# Death Rattles\n\n1. "first"\n2. "second"\n3. "third"\n'
    )
    assert load_death_rattles(path) == ["first", "second", "third"]


def test_parses_bullet_entries(rattles_file):
    path = rattles_file('# Death Rattles\n\n- "first"\n- "second"\n')
    assert load_death_rattles(path) == ["first", "second"]


def test_mixed_bullets_and_numbers_keep_file_order(rattles_file):
    path = rattles_file(
        '# Death Rattles\n'
        '- "bullet one"\n'
        '1. "numbered one"\n'
        '- "bullet two"\n'
        '2. "numbered two"\n'
    )
    assert load_death_rattles(path) == [
        "bullet one",
        "numbered one",
        "bullet two",
        "numbered two",
    ]


@pytest.mark.parametrize(
    "line, expected",
    [
        ('1. "quoted"', "quoted"),
        ("1. unquoted", "unquoted"),
        ('- "quoted bullet"', "quoted bullet"),
        ("- unquoted bullet", "unquoted bullet"),
        # .strip('"') strips *runs* of quotes, not a single balanced pair.
        ('1. ""double wrapped""', "double wrapped"),
        # Unbalanced quotes are stripped from whichever side has them.
        ('1. "only leading', "only leading"),
    ],
)
def test_strips_surrounding_double_quotes(rattles_file, line, expected):
    assert load_death_rattles(rattles_file(line + "\n")) == [expected]


@pytest.mark.parametrize(
    "line, expected",
    [
        ('1. he said "no" loudly', 'he said "no" loudly'),
        ('1. "he said "no" loudly"', 'he said "no" loudly'),
        ('- he said "no" loudly', 'he said "no" loudly'),
    ],
)
def test_internal_quotes_survive(rattles_file, line, expected):
    """Only the outer run of quotes goes; embedded ones are content."""
    assert load_death_rattles(rattles_file(line + "\n")) == [expected]


def test_skips_heading(rattles_file):
    path = rattles_file('# Death Rattles\n1. "kept"\n')
    assert load_death_rattles(path) == ["kept"]


def test_skips_blank_and_whitespace_only_lines(rattles_file):
    path = rattles_file('\n   \n\t\n1. "kept"\n\n     \n')
    assert load_death_rattles(path) == ["kept"]


def test_splits_on_first_period_space_only(rattles_file):
    """The split is ``split(". ", 1)`` — everything after the FIRST ``". "``
    is kept verbatim, internal sentence breaks included. 21 of the 34 real
    entries contain an internal ``". "``, so this is load-bearing.
    """
    path = rattles_file("1. He said 1. no\n")
    assert load_death_rattles(path) == ["He said 1. no"]


def test_multi_sentence_entry_keeps_later_sentences(rattles_file):
    path = rattles_file('3. "I am dying. Who is that beside you? I see a sun."\n')
    assert load_death_rattles(path) == [
        "I am dying. Who is that beside you? I see a sun."
    ]


@pytest.mark.parametrize(
    "line",
    [
        "12.no space",  # digit-led but no ". " anywhere -> skipped
        "1984 was a year",  # digit-led prose without ". " -> skipped
        "not a rattle at all",  # no marker of any kind
        "* star bullet",  # only "- " bullets are recognized
        "#. hash",  # first char is not a digit
    ],
)
def test_non_entry_lines_are_skipped(rattles_file, line):
    assert load_death_rattles(rattles_file(line + "\n")) == []


def test_digit_led_line_with_period_space_is_parsed_even_as_prose(rattles_file):
    """Surprising but current: any digit-led line containing ". " is an entry."""
    path = rattles_file("1984 was a year. So was 1985.\n")
    assert load_death_rattles(path) == ["So was 1985."]


@pytest.mark.parametrize(
    "line, expected",
    [
        ('    1. "indented numbered"', "indented numbered"),
        ('\t- "indented bullet"', "indented bullet"),
        ('  1. "trailing space"   ', "trailing space"),
    ],
)
def test_indented_entries_are_parsed(rattles_file, line, expected):
    """Each line is ``.strip()``ed before dispatch, so indentation is free."""
    assert load_death_rattles(rattles_file(line + "\n")) == [expected]


def test_bullet_prefix_wins_over_numeric(rattles_file):
    """``- 1. foo`` hits the bullet branch first, so the number is retained
    as part of the text rather than being treated as a list marker."""
    path = rattles_file("- 1. foo\n")
    assert load_death_rattles(path) == ["1. foo"]


@pytest.mark.parametrize("line", ["-", "-foo", "--", " - "])
def test_malformed_bullets_are_skipped(rattles_file, line):
    """The branch tests ``startswith("- ")``; a bare ``-`` or a space-less
    ``-foo`` matches neither branch and vanishes silently."""
    assert load_death_rattles(rattles_file(line + "\n")) == []


@pytest.mark.parametrize("line", ['1. ""', '- ""', '1. """"'])
def test_empty_entries_are_kept(rattles_file, line):
    """Empty strings are appended rather than filtered out."""
    assert load_death_rattles(rattles_file(line + "\n")) == [""]


def test_empty_file_yields_no_rattles(rattles_file):
    assert load_death_rattles(rattles_file("")) == []


def test_heading_only_file_yields_no_rattles(rattles_file):
    assert load_death_rattles(rattles_file("# Death Rattles\n")) == []


def test_missing_file_raises_file_not_found(tmp_path):
    """No try/except in the loader — the OSError propagates to the caller."""
    with pytest.raises(FileNotFoundError):
        load_death_rattles(tmp_path / "nope.md")


def test_preserves_unicode_and_em_dashes(rattles_file):
    path = rattles_file(
        '1. "Above silence — dying storms — illuminate the silence"\n'
        '2. "T̈ḧë Ëvërstörm cömës… ✦"\n'
    )
    assert load_death_rattles(path) == [
        "Above silence — dying storms — illuminate the silence",
        "T̈ḧë Ëvërstörm cömës… ✦",
    ]


def test_numbering_gaps_and_reuse_are_ignored(rattles_file):
    """Numbers are never read as ordinals; order is file order."""
    path = rattles_file('7. "a"\n7. "b"\n2. "c"\n999. "d"\n0. "e"\n')
    assert load_death_rattles(path) == ["a", "b", "c", "d", "e"]


def test_crlf_line_endings_leave_no_stray_carriage_returns(rattles_file):
    """splitlines() handles \\r\\n, and .strip() would mop up anything left."""
    path = rattles_file('# Death Rattles\r\n1. "first"\r\n2. "second"\r\n')
    assert load_death_rattles(path) == ["first", "second"]


@pytest.mark.contract
def test_real_file_has_exactly_34_entries(real_rattles_path):
    assert len(load_death_rattles(real_rattles_path)) == 34


@pytest.mark.contract
def test_real_entries_are_clean(real_rattles_path):
    for entry in load_death_rattles(real_rattles_path):
        assert entry, "no empty entries in the shipped file"
        assert not entry.startswith('"'), f"leading quote survived: {entry!r}"
        assert not entry.endswith('"'), f"trailing quote survived: {entry!r}"
        assert not re.match(r"\d+\. ", entry), f"list marker survived: {entry!r}"


# --------------------------------------------------------------- pick_rattle


def test_returns_text_at_chosen_index_and_records_it(rattles):
    rng = ScriptedRandom(2)
    state: dict = {"used_indices": []}
    assert pick_rattle(rattles, state, rng) == "third rattle"
    assert rng.choice_calls == [[0, 1, 2]]
    assert state["used_indices"] == [2]


def test_missing_used_indices_key_defaults_to_empty(rattles, stub_rng):
    state: dict = {}
    assert pick_rattle(rattles, state, stub_rng) == "first rattle"
    # The whole pool was offered, and the key is created on the way out.
    assert stub_rng.choice_calls == [[0, 1, 2]]
    assert state["used_indices"] == [0]


def test_excludes_already_used_indices(rattles, stub_rng):
    state = {"used_indices": [0, 1]}
    assert pick_rattle(rattles, state, stub_rng) == "third rattle"
    assert stub_rng.choice_calls == [[2]]
    assert state["used_indices"] == [0, 1, 2]


def test_mutates_state_in_place_without_touching_disk(rattles, stub_rng, tmp_path):
    """Persistence is the caller's job; pick_rattle only edits the dict."""
    before = sorted(p.name for p in tmp_path.iterdir())
    state: dict = {"used_indices": [], "other": "untouched"}
    pick_rattle(rattles, state, stub_rng)
    assert state["other"] == "untouched"
    assert state["used_indices"] == [0]
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_used_list_identity_is_preserved(rattles, stub_rng):
    """The same list object is mutated and reassigned — callers holding a
    reference (and the ``used.clear()`` on exhaustion) depend on this."""
    used: list[int] = []
    state = {"used_indices": used}
    pick_rattle(rattles, state, stub_rng)
    assert state["used_indices"] is used
    assert used == [0]


def test_exhausted_pool_resets_then_picks(rattles, stub_rng):
    state = {"used_indices": [0, 1, 2]}
    picked = pick_rattle(rattles, state, stub_rng)
    # cleared, then one fresh pick — length 1, not 4.
    assert state["used_indices"] == [rattles.index(picked)]
    assert len(state["used_indices"]) == 1
    assert stub_rng.choice_calls == [[0, 1, 2]]


def test_reset_preserves_the_original_list_object(rattles, stub_rng):
    """``used.clear()`` mutates in place rather than rebinding a new list."""
    used = [0, 1, 2]
    state = {"used_indices": used}
    pick_rattle(rattles, state, stub_rng)
    assert state["used_indices"] is used


def test_full_cycle_before_any_repeat(seeded_rng):
    pool = [f"rattle {i}" for i in range(5)]
    state: dict = {}
    picks = [pick_rattle(pool, state, seeded_rng) for _ in range(2 * len(pool))]
    first, second = picks[: len(pool)], picks[len(pool) :]
    assert sorted(_indices(pool, first)) == list(range(len(pool)))
    assert sorted(_indices(pool, second)) == list(range(len(pool)))


def test_empty_rattles_raises_index_error(stub_rng):
    """``random.choice([])`` raises; nothing guards the empty case."""
    state: dict = {"used_indices": []}
    with pytest.raises(IndexError):
        pick_rattle([], state, stub_rng)


def test_stale_index_beyond_length_is_not_pruned(rattles, stub_rng):
    """A used index left over from a longer file is never cleaned up; it just
    fails to match anything in ``range(len(rattles))`` and rides along."""
    state = {"used_indices": [99]}
    picked = pick_rattle(rattles, state, stub_rng)
    assert stub_rng.choice_calls == [[0, 1, 2]]
    assert state["used_indices"] == [99, rattles.index(picked)]


def test_stale_index_can_trigger_premature_exhaustion(rattles, stub_rng):
    """With every real index used, the stale entry does not stop the reset —
    and the reset wipes the stale entry too."""
    state = {"used_indices": [0, 1, 2, 99]}
    picked = pick_rattle(rattles, state, stub_rng)
    assert state["used_indices"] == [rattles.index(picked)]
    assert 99 not in state["used_indices"]


def test_single_rattle_repeats_forever(stub_rng):
    pool = ["only one"]
    state: dict = {}
    for _ in range(5):
        assert pick_rattle(pool, state, stub_rng) == "only one"
        # Reset fires on every call after the first, so used never grows.
        assert state["used_indices"] == [0]


def test_state_dict_is_the_only_memory(rattles, stub_rng):
    """Two independent states do not see each other's history."""
    a: dict = {"used_indices": [0, 1]}
    b: dict = {"used_indices": []}
    assert pick_rattle(rattles, a, stub_rng) == "third rattle"
    assert pick_rattle(rattles, b, stub_rng) == "first rattle"
    assert a["used_indices"] == [0, 1, 2]
    assert b["used_indices"] == [0]


# ------------------------------------------------------------- format_rattle


@pytest.mark.parametrize(
    "text, expected",
    [
        ("hello", "_hello_"),
        ("", "__"),  # empty input still gets both underscores
        ("  padded  ", "_  padded  _"),  # no trimming
    ],
)
def test_format_rattle_wraps_in_underscores(text, expected):
    assert format_rattle(text) == expected


def test_format_rattle_does_not_escape_existing_underscores():
    """snake_case passes straight through; Slack's mrkdwn is left to cope."""
    assert format_rattle("snake_case_text") == "_snake_case_text_"


def test_format_rattle_leaves_other_markup_alone():
    assert format_rattle('*bold* "quoted" <link>') == '_*bold* "quoted" <link>_'


def test_format_rattle_wraps_each_paragraph_separately():
    """Slack drops inline formatting across a blank line, so one pair of
    underscores spanning both paragraphs would render literally."""
    text = "first para\n\nsecond para"
    assert format_rattle(text) == "_first para_\n\n_second para_"


def test_format_rattle_nests_bold_inside_the_per_line_italics():
    text = "a rattle\n\n*It is Wednesday*"
    assert format_rattle(text) == "_a rattle_\n\n_*It is Wednesday*_"


def test_format_rattle_leaves_blank_and_whitespace_only_lines_untouched():
    assert format_rattle("a\n\nb") == "_a_\n\n_b_"
    assert format_rattle("a\n   \nb") == "_a_\n   \n_b_"


def test_format_rattle_handles_consecutive_content_lines():
    assert format_rattle("one\ntwo") == "_one_\n_two_"

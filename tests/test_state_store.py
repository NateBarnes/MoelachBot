"""Tests for ``StateStore`` and ``new_state``.

``StateStore`` is deliberately dumb: it reads and writes JSON and repairs
nothing. Several tests below pin that non-repair down -- malformed JSON raises
rather than degrading to a default, because every caller already
swallows-and-logs and the original code behaved this way.
"""

from __future__ import annotations

import json

import pytest

from moelachbot.state import StateStore, new_state

DEFAULT_STATE = {"last_reactive": {}, "used_indices": []}


# ------------------------------------------------------------------ new_state


def test_new_state_returns_the_default_shape():
    assert new_state() == DEFAULT_STATE


def test_new_state_returns_a_fresh_object_each_call():
    first = new_state()
    first["last_reactive"]["C111"] = 1.0
    first["used_indices"].append(3)

    assert new_state() == DEFAULT_STATE


# ----------------------------------------------------------------------- load


def test_load_on_missing_file_returns_default_state(store, state_path):
    assert not state_path.exists()

    assert store.load() == DEFAULT_STATE


def test_load_on_missing_file_does_not_create_the_file(store, state_path):
    store.load()

    assert not state_path.exists()


def test_each_load_returns_a_fresh_object(store):
    first = store.load()
    first["last_reactive"]["C111"] = 123.0
    first["used_indices"].append(7)

    assert store.load() == DEFAULT_STATE


def test_each_load_of_an_existing_file_returns_a_fresh_object(store):
    store.save({"last_reactive": {"C111": 1.0}, "used_indices": [0]})

    first = store.load()
    first["used_indices"].append(99)
    second = store.load()

    assert second["used_indices"] == [0]
    assert first is not second


# ----------------------------------------------------------------------- save


def test_save_then_load_round_trip(store):
    state = {"last_reactive": {"C111": 1700000000.5}, "used_indices": [0, 2, 4]}

    store.save(state)

    assert store.load() == state


def test_save_writes_valid_indented_json(store, state_path):
    store.save({"last_reactive": {"C111": 1.0}, "used_indices": [1]})

    raw = state_path.read_text()
    state = {"last_reactive": {"C111": 1.0}, "used_indices": [1]}
    assert json.loads(raw) == state
    # Pin the exact serialization: the file is meant to be human-readable and
    # diffable, and `"  " in raw` is also satisfied by indent=4 or indent=8.
    assert raw == json.dumps(state, indent=2)


def test_save_overwrites_the_file_completely(store):
    store.save({"last_reactive": {"C111": 1.0}, "used_indices": [1, 2, 3], "extra": 1})

    store.save({"used_indices": []})

    # No stale keys survive: save is a full replace, not a merge.
    assert store.load() == {"used_indices": []}


def test_save_creates_the_file_when_missing(store, state_path):
    assert not state_path.exists()

    store.save(new_state())

    assert state_path.exists()


# ------------------------------------------------------- no repair, no normalize


def test_load_raises_on_malformed_json(store, state_path):
    state_path.write_text("{not json at all")

    # Documented current behavior: the store does not repair or fall back to
    # new_state(). Callers are responsible for swallowing-and-logging.
    with pytest.raises(json.JSONDecodeError):
        store.load()


def test_load_raises_on_empty_file(store, state_path):
    state_path.write_text("")

    with pytest.raises(json.JSONDecodeError):
        store.load()


def test_load_preserves_unknown_keys(store, write_state):
    write_state(
        {
            "last_reactive": {"C111": 1.0},
            "used_indices": [0],
            "written_by_a_future_version": {"nested": True},
        }
    )

    loaded = store.load()

    assert loaded["written_by_a_future_version"] == {"nested": True}


def test_load_returns_partial_state_as_is(store, write_state):
    # No normalization: a state file missing "used_indices" comes back missing
    # "used_indices". Callers use setdefault()/get() to cope.
    write_state({"last_reactive": {"C111": 1.0}})

    loaded = store.load()

    assert loaded == {"last_reactive": {"C111": 1.0}}
    assert "used_indices" not in loaded


def test_load_returns_an_empty_object_as_is(store, write_state):
    write_state({})

    assert store.load() == {}


@pytest.mark.parametrize(
    "raw", ["[]", "null", '"a string"', "42"], ids=["list", "null", "string", "number"]
)
def test_load_returns_non_dict_json_as_is(store, state_path, raw):
    # Documented current behavior: json.loads() output is returned untouched,
    # so a non-object top level is not rejected here either.
    state_path.write_text(raw)

    assert store.load() == json.loads(raw)


# ----------------------------------------------------------------------- path


def test_store_exposes_its_path(state_path):
    assert StateStore(state_path).path == state_path


def test_fresh_state_fixture_writes_the_default_state(fresh_state, read_state):
    assert read_state() == DEFAULT_STATE
    assert fresh_state.load() == DEFAULT_STATE

"""Shared fixtures.

Design note: with dependencies injected, almost nothing here is a mock. The
Slack client is autospec'd (method names and signatures are untyped strings at
runtime, so a typo is a production-only crash worth catching); everything else
is a real object or a small hand-written fake. A MagicMock clock would make
cooldown arithmetic pass for the wrong reason.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import random
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import create_autospec

import pytest
from slack_sdk.web.client import WebClient

from moelachbot.config import Config
from moelachbot.core import MoelachBot
from moelachbot.state import StateStore

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------- fakes


class FakeClock:
    """Deterministic clock. Time moves only when sleep()/advance() is called."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._now = float(start)
        self.sleeps: list[float] = []
        # Hook fired *during* a simulated sleep. This is what makes the
        # "channel went on cooldown while we waited" race deterministically
        # testable instead of requiring real threads and timing luck.
        self.on_sleep: Callable[["FakeClock"], None] | None = None

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._now += seconds
        if self.on_sleep is not None:
            self.on_sleep(self)

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def ctime(self, timestamp: float) -> str:
        return f"<t={timestamp}>"


class StubRandom:
    """random.Random stand-in with scripted results and recorded calls."""

    def __init__(
        self, choices: list[int] | None = None, uniform_value: float = 0.0
    ) -> None:
        self._choices = list(choices or [])
        self.uniform_value = uniform_value
        self.choice_calls: list[list[Any]] = []
        self.uniform_calls: list[tuple[float, float]] = []

    def choice(self, seq):
        self.choice_calls.append(list(seq))
        if not seq:
            # Match the stdlib so the empty-rattles test stays honest.
            raise IndexError("Cannot choose from an empty sequence")
        if self._choices:
            want = self._choices.pop(0)
            if want not in seq:
                raise AssertionError(
                    f"scripted choice {want!r} not available in {list(seq)!r}"
                )
            return want
        return seq[0]

    def uniform(self, a, b):
        self.uniform_calls.append((a, b))
        return self.uniform_value


class RecordingRunner:
    """The thread seam. Runs inline by default so tests stay single-threaded."""

    def __init__(self, defer: bool = False) -> None:
        self.defer = defer
        self.jobs: list[Callable[[], None]] = []

    def submit(self, fn: Callable[[], None]) -> None:
        self.jobs.append(fn)
        if not self.defer:
            fn()

    def run_pending(self) -> None:
        while self.jobs:
            self.jobs.pop(0)()


# ------------------------------------------------------------ autouse guards


@pytest.fixture(autouse=True)
def capture_bot_logs(caplog):
    caplog.set_level(logging.DEBUG, logger="moelachbot")
    return caplog


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Hard stop if any test ever dials out."""

    def _boom(*args, **kwargs):
        raise AssertionError("test attempted a real network connection")

    monkeypatch.setattr("socket.socket.connect", _boom)


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    """Any stray relative-path write lands in tmp, never in the repo.

    Deliberately a SUBDIRECTORY of tmp_path, not tmp_path itself. If the cwd
    were the same directory the injected paths live in, a bug that ignored an
    injected path and wrote to a bare relative filename would still land on the
    file under test, and every path-injection test would pass vacuously.
    """
    cwd = tmp_path / "cwd"
    cwd.mkdir(exist_ok=True)
    monkeypatch.chdir(cwd)


# ----------------------------------------------------------------- log / time


@pytest.fixture
def log() -> logging.Logger:
    return logging.getLogger("moelachbot.test")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def stub_rng() -> StubRandom:
    """Scripted RNG — use when a test asserts an exact rattle or delay."""
    return StubRandom()


@pytest.fixture
def seeded_rng() -> random.Random:
    """Real RNG, fixed seed — use for cycling/distribution properties."""
    return random.Random(20240722)


# ---------------------------------------------------------------- slack client


def make_auth_response(
    scopes: str | None = "channels:history,channels:read,chat:write",
):
    """auth.test stand-in. scopes=None omits the header key entirely."""
    headers = {} if scopes is None else {"x-oauth-scopes": scopes}
    return SimpleNamespace(headers=headers, data={"ok": True}, status_code=200)


def make_headerless_response():
    """A response where hasattr(resp, "headers") is genuinely False."""
    return SimpleNamespace(data={"ok": True}, status_code=200)


@pytest.fixture
def auth_response():
    return make_auth_response


@pytest.fixture
def headerless_response():
    return make_headerless_response


@pytest.fixture
def slack_client():
    """Autospec'd WebClient: wrong method names and bad signatures fail loudly."""
    client = create_autospec(WebClient, instance=True, spec_set=True)
    # Never leave auth_test as a bare mock: headers.get() would return a truthy
    # MagicMock whose .split(",") iterates empty, so check_scopes would report
    # every scope missing instead of "could not determine".
    client.auth_test.return_value = make_auth_response()
    client.conversations_list.return_value = {"channels": []}
    client.chat_postMessage.return_value = {"ok": True, "ts": "1700000000.000100"}
    return client


@pytest.fixture
def channel_list():
    """Point conversations_list at a name -> id mapping."""

    def _set(client, mapping: dict[str, str]) -> None:
        client.conversations_list.return_value = {
            "channels": [{"name": n, "id": i} for n, i in mapping.items()]
        }

    return _set


# ----------------------------------------------------------------------- state


@pytest.fixture
def state_path(tmp_path) -> Path:
    return tmp_path / "state.json"


@pytest.fixture
def store(state_path) -> StateStore:
    """A real StateStore on a real temp file: the JSON round-trip is the unit."""
    return StateStore(state_path)


@pytest.fixture
def write_state(state_path):
    def _write(state: dict) -> None:
        state_path.write_text(json.dumps(state))

    return _write


@pytest.fixture
def read_state(state_path):
    def _read() -> dict:
        return json.loads(state_path.read_text())

    return _read


@pytest.fixture
def fresh_state(store) -> StateStore:
    store.save({"last_reactive": {}, "used_indices": []})
    return store


# --------------------------------------------------------------------- rattles

SAMPLE_RATTLES = ["first rattle", "second rattle", "third rattle"]


@pytest.fixture
def rattles() -> list[str]:
    return list(SAMPLE_RATTLES)


@pytest.fixture
def rattles_file(tmp_path):
    """Write a death_rattles.md body and hand back its path."""

    def _write(body: str) -> Path:
        p = tmp_path / "death_rattles.md"
        p.write_text(body)
        return p

    return _write


@pytest.fixture(scope="session")
def real_rattles_path() -> Path:
    return REPO_ROOT / "death_rattles.md"


# ---------------------------------------------------------------------- config

BASE_ENV = {"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_APP_TOKEN": "xapp-test"}


@pytest.fixture
def env():
    """env(COOLDOWN_HOURS=1) -> the required tokens plus overrides."""

    def _env(**overrides) -> dict[str, str]:
        return {**BASE_ENV, **{k: str(v) for k, v in overrides.items()}}

    return _env


@pytest.fixture
def config(env) -> Config:
    return Config.from_env(
        env(MONITORED_CHANNELS="C_MON", SCHEDULE_CHANNEL="C_SCHED")
    )


# ----------------------------------------------------------------- runner / bot


@pytest.fixture
def runner() -> RecordingRunner:
    return RecordingRunner()


@pytest.fixture
def deferred_runner() -> RecordingRunner:
    return RecordingRunner(defer=True)


@pytest.fixture
def make_bot(config, slack_client, store, rattles, clock, stub_rng, runner, log):
    """Build a MoelachBot with test defaults.

        make_bot()
        make_bot(rattles=["only one"])
        make_bot(config_overrides={"cooldown_hours": 1.0})
    """

    def _make(**overrides) -> MoelachBot:
        deps: dict[str, Any] = dict(
            config=config,
            client=slack_client,
            rattles=rattles,
            state_store=store,
            clock=clock,
            rng=stub_rng,
            runner=runner,
            log=log,
        )
        cfg_overrides = overrides.pop("config_overrides", None)
        if cfg_overrides:
            deps["config"] = dataclasses.replace(deps["config"], **cfg_overrides)
        deps.update(overrides)
        return MoelachBot(**deps)

    return _make


@pytest.fixture
def bot(make_bot) -> MoelachBot:
    return make_bot()

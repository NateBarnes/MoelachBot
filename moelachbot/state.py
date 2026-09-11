from __future__ import annotations

import json
from pathlib import Path
from typing import Any

State = dict[str, Any]


def new_state() -> State:
    return {"last_reactive": {}, "used_indices": []}


class StateStore:
    """JSON-file persistence for cooldowns and used-rattle indices.

    Deliberately does not normalize or repair what it reads: malformed JSON
    raises, exactly as before, and every caller already swallows-and-logs.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> State:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return new_state()

    def save(self, state: State) -> None:
        self.path.write_text(json.dumps(state, indent=2))


def channel_on_cooldown(
    channel: str, state: State, *, now: float, cooldown_hours: float
) -> bool:
    """Whether ``channel`` posted recently enough to still be on cooldown.

    ``now`` is passed in rather than read, so a caller can use one timestamp for
    both the check and the subsequent write.
    """
    last = state.get("last_reactive", {}).get(channel)
    if last is None:
        return False
    elapsed_hours = (now - last) / 3600
    return elapsed_hours < cooldown_hours

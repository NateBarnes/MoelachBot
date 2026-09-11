from __future__ import annotations

import random
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def load_death_rattles(path: Path) -> list[str]:
    """Parse Death Rattles out of a Markdown file.

    Accepts either ``- "quote"`` bullets or ``N. "quote"`` numbered entries.
    Body is unchanged from the original; only the path is now a parameter.
    """
    rattles: list[str] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("- "):
            entry = line[2:].strip().strip('"')
            rattles.append(entry)
        elif line and line[0].isdigit() and ". " in line:
            entry = line.split(". ", 1)[1].strip().strip('"')
            rattles.append(entry)
    return rattles


def format_rattle(rattle: str) -> str:
    return f"_{rattle}_"


def pick_rattle(
    rattles: Sequence[str], state: dict[str, Any], rng: random.Random
) -> str:
    """Pick a random Death Rattle, cycling through all before repeating.

    Mutates ``state["used_indices"]`` in place and does NOT persist; callers are
    responsible for saving. That split is preserved from the original.
    """
    used = state.get("used_indices", [])
    available = [i for i in range(len(rattles)) if i not in used]
    if not available:
        used.clear()
        available = list(range(len(rattles)))
    idx = rng.choice(available)
    used.append(idx)
    state["used_indices"] = used
    return rattles[idx]

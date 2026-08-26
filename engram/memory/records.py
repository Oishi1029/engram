"""Pure memory records — no cloud dependency.

Deliberately separate from `store.py`. Retrieval scoring and its tests are pure logic and must
stay runnable with no Firestore client, no credentials and no network. Keeping the dataclasses
here is what lets the load-bearing transfer test run offline on every change, for free.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Episode:
    """One step the agent took, written as it happened."""

    run_id: str
    incident_id: str
    step: int
    thought: str
    tool: str
    tool_args: dict[str, Any]
    observation_summary: str
    created_at: float = field(default_factory=time.time)
    consolidated: bool = False
    episode_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    # 🔴 True when this episode came from a run with memory DISABLED.
    #
    # You only ever disable memory to measure against it, so such a run is a CONTROL, not an
    # experience. Its episodes must never become lessons, or a later run learns the answer to the
    # very incident it is about to be scored on.
    #
    # This exists because relying on the caller to pass `run_ids` was not enough. The Cloud
    # Scheduler job posts an empty body, which means run_ids=None, which takes the unfiltered
    # path — and it quietly swept control-arm episodes into semantic memory on the live service,
    # growing the store from 3 lessons to 8, several of which stated INC-002's answer outright.
    # The README claimed this could not happen. The invariant now holds structurally, whatever
    # the caller passes.
    control_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SemanticMemory:
    """A durable, generalised lesson distilled from one or more episodes.

    `cue` is what the lesson is indexed on — the situation it applies to. `lesson` is the
    transferable content. Keeping them separate is what lets a lesson learned on one service
    fire on a different one.
    """

    cue: str
    lesson: str
    source_run_ids: list[str]
    salience: float = 1.0
    times_retrieved: int = 0
    times_useful: int = 0
    created_at: float = field(default_factory=time.time)
    last_used_at: float = 0.0
    memory_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SemanticMemory":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def text(self) -> str:
        return f"WHEN {self.cue}\nTHEN {self.lesson}"

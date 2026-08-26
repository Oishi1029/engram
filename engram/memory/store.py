"""Firestore access, and the two-tier memory model that is this project's actual contribution.

WHY TWO TIERS
-------------
The common shape for "an agent with memory" is: log every event, then vector-search the log.
That is a transcript, not a memory, and it gets *worse* as it grows — retrieving five useful
items out of ten thousand raw events is a harder problem than retrieving them out of fifty
distilled lessons, and the noise floor rises with volume.

engram separates the two things that are usually conflated:

    EPISODIC   one document per step the agent took. High volume, cheap to write, individually
               low-value. This is what happened.

    SEMANTIC   durable, generalised lessons distilled from episodes by a background pass.
               Low volume, expensive to produce, individually high-value. This is what was learned.

Only semantic memory is retrieved at planning time. Episodic memory exists to be consolidated
*from*, and to make the run auditable on camera.

WHY EVICTION IS NOT OPTIONAL
----------------------------
Every lesson carries a salience score that rises when it is retrieved and contributes to a
successful run, and decays otherwise. Below a floor, it is evicted.

This is load-bearing, not a flourish. Without decay and eviction the semantic tier grows without
bound, retrieval precision falls, and run N+2 becomes worse than run N+1 — which would falsify the
entire claim the project makes. A memory system that cannot forget is a memory system that
degrades.
"""

from __future__ import annotations

import time
from typing import Any, Iterable

from google.cloud import firestore

from engram.config import CONFIG
from engram.memory.records import Episode, SemanticMemory

__all__ = ["Episode", "SemanticMemory", "MemoryStore"]


# --------------------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------------------


class MemoryStore:
    """Thin, explicit Firestore wrapper.

    Deliberately not an ORM. Every read and write here shows up as a visible document change in
    the Firestore console, which is what the demo video points a camera at.
    """

    def __init__(self, client: firestore.Client | None = None) -> None:
        self._db = client or firestore.Client(
            project=CONFIG.require_project(), database=CONFIG.database
        )

    # --- episodic -----------------------------------------------------------------

    def write_episode(self, episode: Episode) -> None:
        self._db.collection(CONFIG.episodic_collection).document(
            episode.episode_id
        ).set(episode.to_dict())

    def unconsolidated_episodes(
        self, limit: int | None = None, run_ids: list[str] | None = None
    ) -> list[Episode]:
        """Episodes not yet consolidated, optionally restricted to specific runs.

        `run_ids` exists for experimental hygiene, not convenience. In the demo the agent runs the
        *same* incident twice — once cold as a control, once warm — and the control run writes
        episodes like any other. Consolidating those would let the warm run learn the answer to the
        very incident it is about to be measured on, which is a leak, not memory. Restricting
        consolidation to the teaching run keeps the comparison honest.
        """
        limit = limit or CONFIG.consolidation_batch
        col = self._db.collection(CONFIG.episodic_collection)
        query = col.where(filter=firestore.FieldFilter("consolidated", "==", False))

        # 🔴 The run filter MUST be pushed into the query, not applied after the limit.
        # It previously fetched `limit` unconsolidated episodes and filtered them in Python. Since
        # the control arm writes its episodes first, the teaching run's episodes fell outside that
        # window — a 12-step teaching run consolidated as 7 episodes, and the step-12
        # apply_remediation outcome, the only thing worth learning, was always among the ones
        # dropped. It looked like a plausible number rather than a bug.
        if run_ids is not None:
            if not run_ids:
                return []
            # Firestore's `in` accepts at most 30 values; chunk and merge above that.
            chunks = [run_ids[i : i + 30] for i in range(0, len(run_ids), 30)]
            out: list[Episode] = []
            known = {f for f in Episode.__dataclass_fields__}
            for chunk in chunks:
                docs = query.where(
                    filter=firestore.FieldFilter("run_id", "in", chunk)
                ).limit(limit).stream()
                for doc in docs:
                    d = doc.to_dict() or {}
                    out.append(Episode(**{k: v for k, v in d.items() if k in known}))
            out.sort(key=lambda e: (e.run_id, e.step))
            return out[:limit]

        known = {f for f in Episode.__dataclass_fields__}
        return [
            Episode(**{k: v for k, v in (doc.to_dict() or {}).items() if k in known})
            for doc in query.limit(limit).stream()
        ]

    def mark_consolidated(self, episode_ids: Iterable[str]) -> None:
        batch = self._db.batch()
        col = self._db.collection(CONFIG.episodic_collection)
        for eid in episode_ids:
            batch.update(col.document(eid), {"consolidated": True})
        batch.commit()

    # --- semantic -----------------------------------------------------------------

    def write_semantic(self, memory: SemanticMemory) -> None:
        self._db.collection(CONFIG.semantic_collection).document(
            memory.memory_id
        ).set(memory.to_dict())

    def all_semantic(self) -> list[SemanticMemory]:
        docs = self._db.collection(CONFIG.semantic_collection).stream()
        return [SemanticMemory.from_dict(d.to_dict() or {}) for d in docs]

    def touch_retrieved(self, memory_ids: Iterable[str]) -> None:
        """Record that these memories were surfaced to the agent."""
        batch = self._db.batch()
        col = self._db.collection(CONFIG.semantic_collection)
        now = time.time()
        for mid in memory_ids:
            batch.update(
                col.document(mid),
                {
                    "times_retrieved": firestore.Increment(1),
                    "last_used_at": now,
                },
            )
        batch.commit()

    def reward(self, memory_ids: Iterable[str], amount: float = 0.5) -> None:
        """Raise salience for memories that contributed to a successful run."""
        batch = self._db.batch()
        col = self._db.collection(CONFIG.semantic_collection)
        for mid in memory_ids:
            batch.update(
                col.document(mid),
                {
                    "salience": firestore.Increment(amount),
                    "times_useful": firestore.Increment(1),
                },
            )
        batch.commit()

    def decay_and_evict(self) -> dict[str, int]:
        """Decay every memory's salience, then evict what falls through the floor.

        Also enforces a hard ceiling on store size by evicting the least salient beyond it, so
        an unusually productive consolidation pass cannot blow up retrieval quality.
        """
        memories = self.all_semantic()
        col = self._db.collection(CONFIG.semantic_collection)

        decayed: list[SemanticMemory] = []
        for m in memories:
            m.salience *= CONFIG.salience_decay
            decayed.append(m)

        evicted = [m for m in decayed if m.salience < CONFIG.eviction_threshold]
        kept = [m for m in decayed if m.salience >= CONFIG.eviction_threshold]

        kept.sort(key=lambda m: m.salience, reverse=True)
        if len(kept) > CONFIG.max_semantic_memories:
            evicted.extend(kept[CONFIG.max_semantic_memories :])
            kept = kept[: CONFIG.max_semantic_memories]

        batch = self._db.batch()
        for m in kept:
            batch.update(col.document(m.memory_id), {"salience": m.salience})
        for m in evicted:
            batch.delete(col.document(m.memory_id))
        batch.commit()

        return {"kept": len(kept), "evicted": len(evicted)}

    # --- runs ---------------------------------------------------------------------

    def write_run(self, run: dict[str, Any]) -> None:
        self._db.collection(CONFIG.runs_collection).document(run["run_id"]).set(run)

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        docs = (
            self._db.collection(CONFIG.runs_collection)
            .order_by("started_at", direction=firestore.Query.DESCENDING)
            .limit(limit)
            .stream()
        )
        return [d.to_dict() or {} for d in docs]

    # --- demo support -------------------------------------------------------------

    def wipe(self) -> dict[str, int]:
        """Delete everything. Used to force a genuinely cold first run on camera."""
        counts: dict[str, int] = {}
        for name in (
            CONFIG.episodic_collection,
            CONFIG.semantic_collection,
            CONFIG.runs_collection,
        ):
            col = self._db.collection(name)
            n = 0
            while True:
                docs = list(col.limit(400).stream())
                if not docs:
                    break
                batch = self._db.batch()
                for d in docs:
                    batch.delete(d.reference)
                batch.commit()
                n += len(docs)
            counts[name] = n
        return counts

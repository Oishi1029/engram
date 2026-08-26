"""The load-bearing test: does a lesson learned on INC-001 fire on INC-002?

This is the project's central claim reduced to an assertion. If a lesson consolidated from the
checkout/Redis incident does not surface when the search/Postgres incident arrives, then there is
no transfer, the warm run has nothing to work with, and the demo has no argument. Everything else
in the system is machinery in service of this one property.

It runs entirely offline — no model calls, no Firestore — so it can be run on every change for free.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engram.env.fixtures import get_incident  # noqa: E402
from engram.memory import retrieval  # noqa: E402
from engram.memory.records import SemanticMemory  # noqa: E402


def _situation(incident_id: str) -> str:
    inc = get_incident(incident_id)
    return f"{inc.title} affected service {inc.affected_service}"


# Lessons of the shape the consolidation prompt asks for: generalised, no service names.
TRANSFERABLE = [
    SemanticMemory(
        cue="latency spikes shortly after a recent deploy to the affected service",
        lesson=(
            "Compare what the deploy changed against the service's connection pool configuration "
            "before investigating the downstream datastore. Raising worker concurrency without "
            "raising the pool starves workers of connections."
        ),
        source_run_ids=["run-cold"],
    ),
    SemanticMemory(
        cue=(
            "a downstream dependency shows elevated p99 latency while its own error rate and cpu "
            "remain normal"
        ),
        lesson=(
            "Treat it as a symptom, not the cause: it is usually absorbing connections held open "
            "by its caller. Do not begin the investigation there."
        ),
        source_run_ids=["run-cold"],
    ),
    SemanticMemory(
        cue="a service reports connection pool timeout or pool exhausted in its logs",
        lesson=(
            "Read the affected service's own logs early; they frequently name the failing "
            "subsystem outright and short-circuit the search."
        ),
        source_run_ids=["run-cold"],
    ),
]

# A control: a real lesson about a genuinely unrelated failure mode. It must NOT outrank the
# transferable ones, or retrieval is just returning whatever is in the store.
DISTRACTORS = [
    SemanticMemory(
        cue="tls certificate expiry causes handshake failures at the edge",
        lesson="Check certificate validity dates and the renewal job.",
        source_run_ids=["run-other"],
        salience=3.0,  # deliberately high — relevance must still beat salience
    ),
    SemanticMemory(
        cue="disk volume reaches capacity and writes begin failing",
        lesson="Check volume utilisation and log rotation settings.",
        source_run_ids=["run-other"],
        salience=3.0,
    ),
]


def main() -> int:
    memories = TRANSFERABLE + DISTRACTORS
    failures: list[str] = []

    query = _situation("INC-002")
    print(f"QUERY (unseen incident):\n  {query}\n")

    results = retrieval.retrieve(query, memories, top_k=5)
    print("RETRIEVED, best first:")
    for m, s in results:
        tag = "TRANSFER" if m in TRANSFERABLE else "distractor"
        print(f"  {s:6.3f}  [{tag}]  WHEN {m.cue[:64]}")
    print()

    if not results:
        failures.append("nothing retrieved at all for the unseen incident")
    else:
        top = results[0][0]
        if top not in TRANSFERABLE:
            failures.append(f"top hit is a distractor, not a transferable lesson: {top.cue!r}")

        retrieved_transfer = sum(1 for m, _ in results if m in TRANSFERABLE)
        if retrieved_transfer < 2:
            failures.append(
                f"only {retrieved_transfer} transferable lesson(s) retrieved; expected at least 2"
            )

    # The deploy lesson is the one that actually solves INC-002. It must be present.
    deploy_lesson = TRANSFERABLE[0]
    if deploy_lesson not in [m for m, _ in results]:
        failures.append("the deploy/pool-size lesson — the one that solves INC-002 — was not retrieved")

    # Sanity: the same lessons must also fire on the incident they came from.
    own = retrieval.retrieve(_situation("INC-001"), memories, top_k=5)
    if not own or own[0][0] not in TRANSFERABLE:
        failures.append("lessons do not even retrieve on their own source incident")

    print("=" * 70)
    if failures:
        print("FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — lessons from INC-001 retrieve on the unseen INC-002, above high-salience distractors.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

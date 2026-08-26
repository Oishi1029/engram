"""The demo. This is the script the video records, unedited, in one take.

THE EXPERIMENT
--------------
    1. BASELINE   Run INC-002 with memory OFF.          <- the control
    2. TEACH      Run INC-001. The agent experiences a pattern for the first time.
    3. CONSOLIDATE  Distil INC-001's episodes into durable lessons. INC-001 ONLY.
    4. WARM       Run INC-002 again, with memory ON.    <- the treatment

Steps 1 and 4 are the SAME INCIDENT with a SINGLE variable changed between them. Everything else
— model, tools, instruction, task, budget — is identical. So any difference is attributable to
memory and nothing else.

Step 3 consolidates the INC-001 run and nothing else. That is not a detail. The baseline in step 1
wrote its own episodes, and sweeping those into memory would let the warm run learn the answer to
the exact incident it is about to be scored on. The whole result would be circular. Restricting
consolidation to the teaching run is what makes the number mean anything.

And INC-001 and INC-002 share no surface detail — different affected service, different datastore,
different culprit, different red herring. So the warm run is not recalling an answer. It is
applying a generalisation to an incident it has never seen.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engram.memory.consolidate import consolidate  # noqa: E402
from engram.memory.store import MemoryStore  # noqa: E402
from engram.runner import run_incident  # noqa: E402

BAR = "=" * 78


def banner(n: int, title: str, subtitle: str = "") -> None:
    print(f"\n{BAR}\n  STEP {n} — {title}")
    if subtitle:
        print(f"  {subtitle}")
    print(BAR)


async def main(wipe: bool) -> int:
    store = MemoryStore()

    if wipe:
        banner(0, "CLEAR ALL MEMORY", "Starting from genuinely nothing, so the baseline is real.")
        print(f"  deleted: {store.wipe()}")

    # ---------------------------------------------------------------- 1. baseline
    banner(
        1,
        "BASELINE — INC-002 with memory OFF",
        "An unfamiliar incident, no prior experience. This is the control.",
    )
    baseline = await run_incident("INC-002", store=store, use_memory=False)

    # ---------------------------------------------------------------- 2. teach
    banner(
        2,
        "EXPERIENCE — INC-001, a DIFFERENT incident",
        "Different service, different datastore, different culprit, different red herring.",
    )
    teaching = await run_incident("INC-001", store=store, use_memory=True)

    # ---------------------------------------------------------------- 3. consolidate
    banner(
        3,
        "CONSOLIDATE — episodes become durable lessons",
        "INC-001's run ONLY. The baseline's episodes are deliberately excluded.",
    )
    summary = consolidate(store, run_ids=[teaching["run_id"]])
    print(f"  episodes consolidated : {summary['episodes']}")
    print(f"  lessons written       : {summary['lessons_written']}")
    print(f"  memories kept/evicted : {summary.get('kept')} / {summary.get('evicted')}")
    for i, lesson in enumerate(summary.get("lessons", []), start=1):
        print(f"\n  [{i}] WHEN {lesson['cue']}")
        print(f"      THEN {lesson['lesson']}")

    # ---------------------------------------------------------------- 4. warm
    banner(
        4,
        "WARM — INC-002 again, with memory ON",
        "Same incident as step 1. Only the memory has changed.",
    )
    warm = await run_incident("INC-002", store=store, use_memory=True)

    # ---------------------------------------------------------------- result
    print(f"\n{BAR}\n  RESULT — INC-002, cold vs warm\n{BAR}")
    rows = [
        ("tool calls", baseline["tool_calls"], warm["tool_calls"], "lower"),
        ("wall seconds", baseline["wall_seconds"], warm["wall_seconds"], "lower"),
        ("total tokens", baseline["total_tokens"], warm["total_tokens"], "lower"),
        (
            "steps on red herring",
            baseline["wasted_steps_on_red_herring"],
            warm["wasted_steps_on_red_herring"],
            "lower",
        ),
        (
            "diagnosis correct",
            "YES" if baseline["correct"] else "NO",
            "YES" if warm["correct"] else "NO",
            "",
        ),
        (
            "remediation chosen",
            baseline.get("chosen_remediation") or "(none)",
            warm.get("chosen_remediation") or "(none)",
            "",
        ),
        (
            "INCIDENT RESOLVED",
            "YES" if baseline.get("resolved") else "NO",
            "YES" if warm.get("resolved") else "NO",
            "",
        ),
    ]
    print(f"  {'metric':<24}{'COLD':>30}{'WARM':>30}{'change':>10}")
    print(f"  {'-' * 94}")
    for name, cold, hot, better in rows:
        if better == "lower" and isinstance(cold, (int, float)) and isinstance(hot, (int, float)):
            if cold:
                delta = f"{(hot - cold) / cold * 100:+.0f}%"
            else:
                delta = "n/a" if hot == 0 else f"+{hot}"
        else:
            delta = ""
        print(f"  {name:<24}{cold!s:>30}{hot!s:>30}{delta:>10}")

    print(f"\n  memories recalled on the warm run: {warm['memories_used_count']}")
    if baseline.get("outcome"):
        print(f"\n  COLD outcome: {baseline['outcome'].splitlines()[0]}")
    if warm.get("outcome"):
        print(f"  WARM outcome: {warm['outcome'].splitlines()[0]}")
    print(f"  cold path: {' -> '.join(baseline['services_investigated'])}")
    print(f"  warm path: {' -> '.join(warm['services_investigated'])}")
    print(BAR)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-wipe", action="store_true", help="keep existing memory")
    a = ap.parse_args()
    raise SystemExit(asyncio.run(main(wipe=not a.no_wipe)))

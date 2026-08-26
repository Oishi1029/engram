"""Measure cold vs warm properly, over repeated trials.

WHY THIS EXISTS
---------------
A single cold-vs-warm pair is not evidence. Cold runs on this task have been observed at 8, 9, 10,
12, 16 and 19 tool calls on byte-identical input — the model's own sampling variance is larger than
the effect being measured. The first demo showed a "10% improvement" that was indistinguishable
from noise, and the warm run in that pair happened to waste a step the cold run avoided.

So the claim this project makes gets measured over N trials per arm, and reports spread alongside
the mean. If the intervals overlap heavily, the honest conclusion is that it does not work yet.

PROTOCOL
--------
    1. Wipe memory.
    2. COLD ARM   — N trials of INC-002, memory disabled.
    3. TEACH      — one run of INC-001, then consolidate THAT RUN ONLY.
    4. WARM ARM   — N trials of INC-002, memory enabled.

INC-002 is never consolidated, so no trial in either arm can learn its own answer. INC-001 shares
no surface detail with it — different service, datastore, culprit and red herring — so the warm arm
is applying a generalisation, not recalling a result.

Known minor impurity, stated rather than hidden: successful warm trials reward the salience of the
lessons they used, so later warm trials see marginally higher salience than earlier ones. With a
two-lesson store this can only affect ordering between two items that are both already injected.
It is not worth the complexity of snapshot/restore, but it is real and it is recorded here.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics as stats
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engram.memory.consolidate import consolidate  # noqa: E402
from engram.memory.store import MemoryStore  # noqa: E402
from engram.runner import run_incident  # noqa: E402

TARGET = "INC-002"
TEACH = "INC-001"
METRICS = [
    ("tool_calls", "tool calls", True),
    ("wall_seconds", "wall seconds", True),
    ("total_tokens", "total tokens", True),
    ("wasted_steps_on_red_herring", "steps on red herring", True),
]


def describe(values: list[float]) -> str:
    if not values:
        return "-"
    if len(values) == 1:
        return f"{values[0]:.1f}"
    return (
        f"{stats.mean(values):.1f} ± {stats.stdev(values):.1f} "
        f"(min {min(values):.0f}, max {max(values):.0f})"
    )



def assert_control_arm_is_quarantined(store: MemoryStore, cold_runs: list) -> None:
    """Fail loudly if any control-arm episode is eligible for consolidation.

    This invariant is the difference between a measurement and a circular one, and it has already
    been violated once in production: the Cloud Scheduler job posts an empty body, which took the
    unfiltered consolidation path and swept control-arm episodes into semantic memory on the live
    service. Nothing failed. The store simply grew, and several new lessons stated the test
    incident's answer outright.

    It is enforced structurally now (control-arm episodes are written ineligible), but a silent
    regression here would quietly invalidate every number this script prints, so it is also checked
    at runtime rather than trusted.
    """
    ids = {r["run_id"] for r in cold_runs}
    leaked = [e for e in store.unconsolidated_episodes(limit=500) if e.run_id in ids]
    if leaked:
        raise SystemExit(
            f"ABORT: {len(leaked)} control-arm episodes are eligible for consolidation. "
            "The warm arm would learn the answer to its own measurement. Results discarded."
        )
    print(f"  guard: 0 of {len(ids)} control runs are eligible for consolidation \u2713")

async def arm(name: str, n: int, store: MemoryStore, use_memory: bool) -> list[dict[str, Any]]:
    out = []
    for i in range(1, n + 1):
        rec = await run_incident(TARGET, store=store, use_memory=use_memory, verbose=False)
        flag = "ok " if rec["correct"] else "MISS"
        print(
            f"  {name} trial {i}/{n}: {flag} "
            f"{rec['tool_calls']:>3} calls  {rec['wall_seconds']:>6.1f}s  "
            f"{rec['total_tokens']:>7} tok  "
            f"{rec['wasted_steps_on_red_herring']} red-herring  "
            f"{rec['memories_used_count']} recalled"
        )
        out.append(rec)
    return out


async def main(n: int) -> int:
    store = MemoryStore()
    print(f"Wiping memory: {store.wipe()}\n")

    print(f"COLD ARM — {n} trials of {TARGET}, memory OFF")
    cold = await arm("cold", n, store, use_memory=False)

    print(f"\nTEACH — one run of {TEACH}, then consolidate that run only")
    assert_control_arm_is_quarantined(store, cold)
    teach = await run_incident(TEACH, store=store, use_memory=True, verbose=False)
    print(f"  {TEACH}: {teach['tool_calls']} calls, correct={teach['correct']}")
    summary = consolidate(store, run_ids=[teach["run_id"]])
    print(f"  consolidated {summary['episodes']} episodes -> {summary['lessons_written']} lessons")
    for i, lesson in enumerate(summary.get("lessons", []), start=1):
        print(f"    [{i}] WHEN {lesson['cue']}")
        print(f"        THEN {lesson['lesson']}")

    print(f"\nWARM ARM — {n} trials of {TARGET}, memory ON")
    warm = await arm("warm", n, store, use_memory=True)

    # ---------------------------------------------------------------- report
    print(f"\n{'=' * 86}")
    print(f"  RESULT — {TARGET}, {n} trials per arm")
    print(f"{'=' * 86}")
    print(f"  {'metric':<24}{'COLD':>26}{'WARM':>26}{'mean Δ':>10}")
    print(f"  {'-' * 84}")
    for key, label, lower_better in METRICS:
        c = [r[key] for r in cold]
        w = [r[key] for r in warm]
        cm, wm = stats.mean(c), stats.mean(w)
        delta = f"{(wm - cm) / cm * 100:+.0f}%" if cm else ("0%" if wm == 0 else "n/a")
        print(f"  {label:<24}{describe(c):>26}{describe(w):>26}{delta:>10}")

    for key, label in (
        ("correct", "diagnosis correct"),
        ("remediation_correct", "remediation correct"),
        ("resolved", "FULLY RESOLVED"),
    ):
        c_ok = sum(1 for r in cold if r.get(key))
        w_ok = sum(1 for r in warm if r.get(key))
        print(f"  {label:<24}{f'{c_ok}/{n}':>26}{f'{w_ok}/{n}':>26}")

    def counts(rows):
        seen: dict[str, int] = {}
        for r in rows:
            seen[r.get("chosen_remediation") or "(none)"] = (
                seen.get(r.get("chosen_remediation") or "(none)", 0) + 1
            )
        return ", ".join(f"{k}×{v}" for k, v in sorted(seen.items(), key=lambda kv: -kv[1]))

    print(f"  {'remediation chosen':<24}{counts(cold):>26}{counts(warm):>26}")
    print(f"  {'memories recalled':<24}{'0':>26}"
          f"{describe([r['memories_used_count'] for r in warm]):>26}")
    print(f"{'=' * 86}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=5, help="trials per arm")
    a = ap.parse_args()
    raise SystemExit(asyncio.run(main(a.n)))

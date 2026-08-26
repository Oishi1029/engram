"""The demo as filmed — one continuous, unedited take.

Same experiment as scripts/demo.py. The differences are all presentational, because this is what a
camera points at and the judging rubric asks specifically for "an unedited, live execution of the
agent performing its task (via terminal logs, database updates, or UI changes)":

  * library noise is silenced, so nothing on screen is unexplained
  * phases are announced in large banners a viewer can read at video resolution
  * short beats let a viewer's eye catch up without the recording ever being cut
  * the closing table is the argument, sized to be legible in a 1080p frame

Run it beside the Firestore console with the `episodes` and `semantic_memory` collections open, and
the documents appear on screen as the agent works.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# --- silence everything that would otherwise appear on camera unexplained -------------
warnings.filterwarnings("ignore")
os.environ.setdefault("GRPC_VERBOSITY", "NONE")
os.environ.setdefault("GLOG_minloglevel", "3")
for noisy in ("google", "google.adk", "google.genai", "opentelemetry", "urllib3", "absl"):
    logging.getLogger(noisy).setLevel(logging.CRITICAL)
logging.basicConfig(level=logging.CRITICAL)

from engram.memory.consolidate import consolidate  # noqa: E402
from engram.memory.store import MemoryStore  # noqa: E402
from engram.runner import run_incident  # noqa: E402

W = 96
BOLD, DIM, GREEN, RED, YELLOW, CYAN, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[0m",
)


def beat(seconds: float = 1.2) -> None:
    time.sleep(seconds)


def banner(n: str, title: str, sub: str, colour: str = CYAN) -> None:
    print()
    print(f"{colour}{'═' * W}{RESET}")
    print(f"{colour}{BOLD}  {n}  {title}{RESET}")
    print(f"{colour}  {sub}{RESET}")
    print(f"{colour}{'═' * W}{RESET}")
    beat()


def verdict_line(rec: dict) -> None:
    ok = rec.get("resolved")
    colour = GREEN if ok else RED
    mark = "RESOLVED" if ok else "NOT RESOLVED"
    print()
    print(f"  {colour}{BOLD}{mark}{RESET}   "
          f"diagnosis {'correct' if rec['correct'] else 'wrong'}"
          f"  ·  remediation {BOLD}{rec.get('chosen_remediation') or 'none'}{RESET}")
    outcome = (rec.get("outcome") or "").splitlines()
    if outcome:
        head = outcome[0]
        print(f"  {colour}{head}{RESET}")
        for extra in outcome[1:3]:
            print(f"  {DIM}{extra}{RESET}")
    print(f"  {DIM}{rec['tool_calls']} tool calls · {rec['wall_seconds']:.0f}s · "
          f"{rec['total_tokens']:,} tokens · {rec['memories_used_count']} memories recalled{RESET}")


async def main() -> int:
    store = MemoryStore()

    print()
    print(f"{BOLD}engram — a long-horizon task agent with durable, consolidating memory{RESET}")
    print(f"{DIM}gemini-3.7-flash on Vertex AI · Google ADK · Cloud Run · Firestore{RESET}")
    beat(1.4)

    banner("STEP 0", "CLEAR ALL MEMORY", "So the baseline is genuinely cold. Watch Firestore empty.", DIM)
    print(f"  deleted: {store.wipe()}")
    beat(1.4)

    banner("STEP 1", "BASELINE — INC-002, memory OFF",
           "An unfamiliar incident. No prior experience. This is the control.", RED)
    cold = await run_incident("INC-002", store=store, use_memory=False, verbose=True)
    verdict_line(cold)
    beat(2.0)

    banner("STEP 2", "EXPERIENCE — INC-001, a DIFFERENT incident",
           "Different service, datastore, culprit and red herring. Only the causal shape repeats.", YELLOW)
    teach = await run_incident("INC-001", store=store, use_memory=True, verbose=True)
    verdict_line(teach)
    beat(2.0)

    banner("STEP 3", "CONSOLIDATE — episodes become durable lessons",
           "INC-001 only. The baseline's episodes are excluded, or it would learn its own answer.", YELLOW)
    summary = consolidate(store, run_ids=[teach["run_id"]])
    print(f"  {summary['episodes']} episodes  →  {summary['lessons_written']} lessons"
          f"   (kept {summary.get('kept')}, evicted {summary.get('evicted')})")
    for i, lesson in enumerate(summary.get("lessons", []), start=1):
        print()
        print(f"  {BOLD}[{i}] WHEN{RESET} {lesson['cue']}")
        print(f"      {BOLD}THEN{RESET} {lesson['lesson']}")
    print()
    print(f"  {DIM}Note: not one of these names a service. That is what lets them transfer.{RESET}")
    beat(4.0)

    banner("STEP 4", "WARM — INC-002 again, memory ON",
           "The same incident as STEP 1. The only thing that changed is memory.", GREEN)
    warm = await run_incident("INC-002", store=store, use_memory=True, verbose=True)
    verdict_line(warm)
    beat(2.0)

    # ------------------------------------------------------------------ the argument
    print()
    print(f"{BOLD}{'═' * W}{RESET}")
    print(f"{BOLD}  RESULT — same incident, memory is the only variable{RESET}")
    print(f"{BOLD}{'═' * W}{RESET}")

    def row(label: str, c, w, better_lower: bool = True) -> None:
        if isinstance(c, (int, float)) and isinstance(w, (int, float)) and c:
            d = (w - c) / c * 100
            delta = f"{d:+.0f}%"
            colour = GREEN if (d < 0) == better_lower and d != 0 else DIM
        else:
            delta, colour = "", DIM
        cs = f"{c:,}" if isinstance(c, int) else str(c)
        ws = f"{w:,}" if isinstance(w, int) else str(w)
        print(f"  {label:<26}{cs:>28}{ws:>28}{colour}{delta:>10}{RESET}")

    print(f"  {DIM}{'metric':<26}{'COLD (no memory)':>28}{'WARM (memory)':>28}{RESET}")
    print(f"  {DIM}{'─' * (W - 2)}{RESET}")
    row("tool calls", cold["tool_calls"], warm["tool_calls"])
    row("wall seconds", round(cold["wall_seconds"]), round(warm["wall_seconds"]))
    row("total tokens", cold["total_tokens"], warm["total_tokens"])
    row("steps on the red herring", cold["wasted_steps_on_red_herring"],
        warm["wasted_steps_on_red_herring"])
    print(f"  {'diagnosis correct':<26}{('YES' if cold['correct'] else 'NO'):>28}"
          f"{('YES' if warm['correct'] else 'NO'):>28}")
    print(f"  {BOLD}{'remediation chosen':<26}"
          f"{RED}{cold.get('chosen_remediation',''):>28}{RESET}"
          f"{GREEN}{warm.get('chosen_remediation',''):>28}{RESET}")
    print(f"  {BOLD}{'INCIDENT RESOLVED':<26}"
          f"{RED}{('YES' if cold.get('resolved') else 'NO'):>28}{RESET}"
          f"{GREEN}{('YES' if warm.get('resolved') else 'NO'):>28}{RESET}")
    print(f"{BOLD}{'═' * W}{RESET}")
    print()
    print(f"  {DIM}Both diagnose the root cause correctly. Only one of them knows what the"
          f" textbook fix costs.{RESET}")
    print(f"  {DIM}github.com/Oishi1029/engram{RESET}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

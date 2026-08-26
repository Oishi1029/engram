"""The consolidation pass — episodic experience becomes durable, transferable knowledge.

This runs OUT OF BAND, not on the agent's critical path. That is a deliberate architectural
position and it is the one this project is really arguing for: the expensive act of working out
what an experience *meant* should not be paid for during the task, in the same way that a human
does not consolidate memory while still handling the incident.

Concretely, in deployment this is a Cloud Run endpoint invoked by Cloud Scheduler, so the agent's
own latency is untouched by it.

THE PROMPT IS THE PRODUCT
-------------------------
Everything here turns on one instruction: a lesson must be written so it fires on an incident that
shares the *causal shape* but none of the surface details. A lesson that says "check redis-session
on checkout-api" is worthless on a Postgres incident in the search path. A lesson that says "after
a deploy that raises concurrency, compare it against the connection pool size before investigating
the downstream datastore" transfers.

Getting that generalisation right is the difference between a memory system and a cache, and it is
what the second run has to prove on camera.
"""

from __future__ import annotations

import functools
import json
import logging
from typing import Any

from google import genai
from google.genai import types

from engram.config import CONFIG
from engram.memory.records import Episode, SemanticMemory
from engram.memory.store import MemoryStore

log = logging.getLogger(__name__)


CONSOLIDATION_INSTRUCTION = """\
You are the memory consolidation process for an autonomous incident-response agent.

You are given the full step-by-step trace of one or more completed investigations. Your job is to
distil DURABLE, TRANSFERABLE LESSONS that will make future investigations faster and more accurate.

THE ONE RULE THAT MATTERS:
A lesson must be written so that it fires on a FUTURE incident that shares the underlying causal
shape but NONE of the surface details — different service, different datastore, different symptom,
different team.

Concretely:
  BAD  (useless on any other incident):
       "checkout-api had redis.pool_size 10 with worker_concurrency 60, so raise the pool."
  GOOD (transfers to an unseen incident):
       "When latency spikes shortly after a deploy, compare what that deploy changed against the
        service's connection-pool configuration before investigating the downstream datastore."

Also capture DEAD ENDS. Knowing which investigation path wasted steps is as valuable as knowing
which one worked, because avoiding it is what makes the next run shorter:
  GOOD: "A downstream dependency showing elevated p99 while its error rate and CPU stay normal is
         usually absorbing a caller's held-open connections. It is a symptom. Do not start there."

For each lesson output an object with:
  "cue"    - the situation this applies to, written in general terms. This is what the lesson is
             matched on, so use the vocabulary a future incident would use: symptoms, timing,
             signal shapes. NEVER name a specific service.
  "lesson" - the transferable guidance. Imperative, one or two sentences. Concrete about what to
             check and in what order. NEVER name a specific service.

Produce AT MOST 4 lessons. Fewer, sharper lessons beat more, vaguer ones — this memory is capped
and every weak lesson displaces a strong one.

Return ONLY a JSON object of the form: {"lessons": [{"cue": "...", "lesson": "..."}]}
"""


def _format_episodes(episodes: list[Episode]) -> str:
    """Render episodes into a trace, grouped by run."""
    by_run: dict[str, list[Episode]] = {}
    for e in episodes:
        by_run.setdefault(e.run_id, []).append(e)

    blocks: list[str] = []
    for run_id, eps in by_run.items():
        eps.sort(key=lambda e: e.step)
        incident = eps[0].incident_id if eps else "unknown"
        lines = [f"--- INVESTIGATION {run_id} (incident {incident}) ---"]
        for e in eps:
            lines.append(f"  step {e.step}: {e.thought}")
            lines.append(f"    action: {e.tool}({json.dumps(e.tool_args)})")
            lines.append(f"    observed: {e.observation_summary}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


@functools.lru_cache(maxsize=1)
def _client() -> genai.Client:
    """A Vertex AI-backed GenAI client, created once and reused.

    Vertex rather than the Gemini Developer API, for two independent reasons: it keeps every call
    inside the Google Cloud project (so deployment is provable from one console screen), and Google
    excludes "Gemini API in AI Studio" from free-trial credit while Vertex is not on that list.

    🔴 THE CACHING IS A CORRECTNESS FIX, NOT AN OPTIMISATION.
    This was previously called inline as `_client().models.generate_content(...)`, which makes the
    Client a temporary: nothing holds a reference to it once `.models` is bound. The SDK's
    `SyncHttpxClient` defines a `__del__` that closes the underlying socket, so the client could be
    collected and closed *while its own request was in flight*, surfacing as

        RuntimeError: Cannot send a request, as the client has been closed.

    It reproduced only after several agent runs had raised enough garbage-collection pressure,
    which made it look like state corruption from the ADK runner rather than a dangling reference.
    Holding the client for the process lifetime removes the failure entirely, and reusing one
    connection pool is what you want on Cloud Run in any case.
    """
    return genai.Client(
        vertexai=True,
        project=CONFIG.require_project(),
        location=CONFIG.location,
    )


def _parse_lessons(raw: str) -> list[dict[str, str]]:
    """Parse the model's JSON, tolerating a stray code fence."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        log.warning("consolidation returned unparseable JSON; skipping this batch")
        return []

    lessons = payload.get("lessons", []) if isinstance(payload, dict) else []
    out: list[dict[str, str]] = []
    for item in lessons:
        if not isinstance(item, dict):
            continue
        cue = str(item.get("cue", "")).strip()
        lesson = str(item.get("lesson", "")).strip()
        if cue and lesson:
            out.append({"cue": cue, "lesson": lesson})
    return out


def consolidate(
    store: MemoryStore, limit: int | None = None, run_ids: list[str] | None = None
) -> dict[str, Any]:
    """Run one consolidation pass. Returns a summary suitable for logging or an HTTP response.

    `run_ids` restricts which runs are consolidated. See `MemoryStore.unconsolidated_episodes`
    for why that matters: it is what stops the demo leaking an answer into its own measurement.
    """
    episodes = store.unconsolidated_episodes(limit=limit, run_ids=run_ids)
    if not episodes:
        return {"status": "nothing_to_consolidate", "episodes": 0, "lessons_written": 0}

    trace = _format_episodes(episodes)

    response = _client().models.generate_content(
        model=CONFIG.model,
        contents=trace,
        config=types.GenerateContentConfig(
            system_instruction=CONSOLIDATION_INSTRUCTION,
            response_mime_type="application/json",
            temperature=0.2,
            max_output_tokens=2048,
        ),
    )

    lessons = _parse_lessons(response.text or "")
    run_ids = sorted({e.run_id for e in episodes})

    written = 0
    for item in lessons:
        store.write_semantic(
            SemanticMemory(cue=item["cue"], lesson=item["lesson"], source_run_ids=run_ids)
        )
        written += 1

    store.mark_consolidated([e.episode_id for e in episodes])
    housekeeping = store.decay_and_evict()

    return {
        "status": "ok",
        "episodes": len(episodes),
        "runs": run_ids,
        "lessons_written": written,
        "lessons": lessons,
        **housekeeping,
    }

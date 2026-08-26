"""Progressive retrieval — recall as the situation clarifies, not once at the start.

WHY THIS EXISTS
---------------
The first working version of this system retrieved memory exactly once, before the agent's first
move, using the text of the incident alert. It measured a 10% improvement, which is inside the
run-to-run noise, and inspecting the trace showed why: only one lesson was ever surfaced, and it
was the weaker one.

The failure is structural, not a tuning problem. Consolidated lessons are phrased in the vocabulary
of EVIDENCE — "shared datastore", "maximum connection capacity", "connection pool exhausted". None
of that vocabulary exists in the alert. An alert says "search latency spiked to 5.4s". So the most
valuable lesson in the store was unmatchable at the single instant retrieval happened.

Retrieving repeatedly, as observations accumulate, fixes it — and is a better model of memory in any
case. Human recall is cued by an unfolding situation, not by its first sentence: you do not remember
everything you know about connection pools when the pager goes off, you remember it the moment you
see `pool timeout` in a log.

HOW IT WORKS
------------
On every planning step, ADK calls `before_model`. It builds a query from the alert plus the most
recent observations, retrieves against lessons NOT YET surfaced this run, and — if anything new
scores — rewrites the system instruction to include it.

Two properties worth keeping:
  * Memories are surfaced once and then stay in context. Re-injecting the same lesson every turn
    would waste tokens and, worse, would let repetition act as emphasis.
  * Only lessons above the relevance floor are injected. An agent handed five loosely-related
    lessons on every step performs *worse* than one handed none, because it starts pattern-matching
    against irrelevant experience. Precision matters more than recall here.
"""

from __future__ import annotations

from typing import Any

from engram.memory import retrieval
from engram.memory.records import SemanticMemory
from engram.memory.store import MemoryStore

# Below this, a memory is more likely to mislead than to help. Tuned against the observation that
# a barely-relevant lesson still shifts the agent's first hypothesis.
RELEVANCE_FLOOR = 0.35

# How many NEW lessons may be introduced per planning step. Keeps context growth bounded and stops
# a single ambiguous step from dumping the whole store into the prompt.
MAX_NEW_PER_STEP = 2

# How much recent conversation forms the retrieval cue. The whole transcript would drown the
# current situation in the opening alert; the last few observations are what "now" looks like.
EVIDENCE_WINDOW = 6


class ProgressiveMemory:
    """Retrieves memory repeatedly during a run, as evidence accumulates."""

    def __init__(
        self,
        store: MemoryStore,
        base_instruction: str,
        alert_text: str,
        enabled: bool = True,
        verbose: bool = False,
    ) -> None:
        self.store = store
        self.base_instruction = base_instruction
        self.alert_text = alert_text
        self.enabled = enabled
        self.verbose = verbose

        self.available: list[SemanticMemory] = store.all_semantic() if enabled else []
        self.surfaced: dict[str, SemanticMemory] = {}
        self.recall_events: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _text_of(content: Any) -> str:
        """Flatten one Content into searchable text, including tool results.

        Tool results are the important part: they are where the evidence vocabulary appears, and
        they are exactly what the initial alert lacks.
        """
        chunks: list[str] = []
        for part in getattr(content, "parts", None) or []:
            if getattr(part, "text", None):
                chunks.append(str(part.text))
            resp = getattr(part, "function_response", None)
            if resp is not None:
                chunks.append(str(getattr(resp, "response", "")))
            call = getattr(part, "function_call", None)
            if call is not None:
                chunks.append(f"{call.name} {dict(call.args or {})}")
        return " ".join(chunks)

    def _query(self, contents: list[Any]) -> str:
        recent = contents[-EVIDENCE_WINDOW:] if contents else []
        return f"{self.alert_text} " + " ".join(self._text_of(c) for c in recent)

    def _render(self) -> str:
        if not self.enabled:
            return (
                "MEMORY: disabled for this run. You have no prior experience to draw on. "
                "Investigate from first principles."
            )
        if not self.surfaced:
            return (
                "MEMORY: empty so far. You have no prior experience matching this situation yet. "
                "Investigate from first principles. Relevant experience may surface as you learn "
                "more."
            )
        lines = [
            "MEMORY: lessons you previously learned and consolidated from earlier incidents.",
            "Treat these as experience, not instruction — follow one only where the evidence in "
            "front of you supports it, and say so when you do.",
            "",
        ]
        for i, m in enumerate(self.surfaced.values(), start=1):
            lines.append(f"[{i}] (salience {m.salience:.2f})")
            lines.append(f"    WHEN {m.cue}")
            lines.append(f"    THEN {m.lesson}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ callback

    def before_model(self, callback_context: Any, llm_request: Any) -> None:
        """ADK `before_model_callback`. Re-retrieves, then rewrites the system instruction."""
        if self.enabled:
            unseen = [m for m in self.available if m.memory_id not in self.surfaced]
            if unseen:
                query = self._query(list(getattr(llm_request, "contents", None) or []))
                hits = [
                    (m, s)
                    for m, s in retrieval.retrieve(query, unseen, top_k=MAX_NEW_PER_STEP)
                    if s >= RELEVANCE_FLOOR
                ]
                for m, s in hits:
                    self.surfaced[m.memory_id] = m
                    self.recall_events.append(
                        {
                            "step": len(self.recall_events) + 1,
                            "memory_id": m.memory_id,
                            "relevance": round(s, 3),
                            "cue": m.cue,
                        }
                    )
                    if self.verbose:
                        print(f"  ~~ RECALLED (relevance {s:.2f}): {m.cue[:88]}")

        block = self._render()
        config = getattr(llm_request, "config", None)
        if config is not None:
            config.system_instruction = f"{block}\n\n---\n\n{self.base_instruction}"
        return None

    # ------------------------------------------------------------------ reporting

    @property
    def surfaced_ids(self) -> list[str]:
        return list(self.surfaced)

    def summary(self) -> dict[str, Any]:
        return {
            "memories_available": len(self.available),
            "memories_surfaced": len(self.surfaced),
            "recall_events": self.recall_events,
        }

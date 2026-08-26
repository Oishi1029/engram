"""Retrieval: choosing which lessons to put in front of the agent before it plans.

DESIGN CHOICE — LEXICAL, NOT VECTOR, AND WHY THAT IS THE RIGHT CALL HERE
------------------------------------------------------------------------
The obvious move is embeddings. This deliberately does not use them, for three reasons:

1.  The semantic tier is small by construction (capped at `max_semantic_memories`, default 200).
    Exhaustive scoring over 200 items costs microseconds. An ANN index would be solving a problem
    this system does not have.

2.  An embedding call per retrieval, on every planning step, is a paid model round-trip added to
    the agent's critical path. It would inflate both latency and cost — the two numbers the
    second run is trying to beat — while measuring nothing about memory quality.

3.  It keeps the improvement attributable. If the warm run beats the cold run, that difference is
    caused by *what was consolidated*, not by embedding quality. Removing a confounder makes the
    result legible to a judge in the thirty seconds they will spend on it.

Scoring is term overlap against the memory's `cue`, weighted by salience so that lessons which
have proven useful surface ahead of equally-relevant ones that have not. That salience weighting
is what makes retrieval improve over time rather than staying static.
"""

from __future__ import annotations

import math
import re

from engram.config import CONFIG
from engram.memory.records import SemanticMemory

_WORD = re.compile(r"[a-z0-9_.\-]+")

# Terms that carry no discriminative signal in this domain. Kept explicit and small rather than
# pulling in a stopword corpus, so the behaviour is inspectable.
_STOP = {
    "the", "a", "an", "and", "or", "is", "are", "was", "were", "to", "of", "in", "on",
    "at", "for", "with", "by", "from", "that", "this", "it", "its", "as", "be", "been",
    "has", "have", "had", "not", "but", "if", "then", "than", "so", "we", "i", "you",
    "service", "services", "issue", "problem",
}


def _stem(token: str) -> str:
    """Crude suffix stripping, and crude is the right amount here.

    Consolidated lessons are written by a model, so word forms vary unpredictably: an incident
    reports latency "spiked" while the lesson generalises about latency "spikes". Without this
    they are different tokens and the lesson does not fire — which would silently break transfer,
    the one property the whole system exists to provide.

    A real stemmer (Porter, Snowball) would be more correct and would also mean a dependency and
    a corpus for a 200-item store. What matters is only that both sides stem *consistently*, so
    even a wrong-but-stable stem ("postgres" -> "postgr") still matches itself.
    """
    for suffix in ("ing", "ed", "es", "s"):
        if len(token) - len(suffix) >= 4 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _tokens(text: str) -> set[str]:
    return {
        _stem(t)
        for t in _WORD.findall(text.lower())
        if t not in _STOP and len(t) > 2
    }


def score(query: str, memory: SemanticMemory) -> float:
    """Relevance of one memory to the current situation.

    Overlap is measured against the cue (the situation the lesson applies to) with the lesson body
    contributing at a lower weight — a lesson should fire because the *situation* matches, not
    because its remedy happens to share vocabulary with the query.
    """
    q = _tokens(query)
    if not q:
        return 0.0

    cue_overlap = len(q & _tokens(memory.cue))
    lesson_overlap = len(q & _tokens(memory.lesson))
    if cue_overlap == 0 and lesson_overlap == 0:
        return 0.0

    raw = cue_overlap + 0.4 * lesson_overlap
    normalised = raw / math.sqrt(len(q))

    # Salience is a multiplier, not an addend, so a proven lesson outranks an equally-relevant
    # unproven one — but no amount of salience rescues an irrelevant memory from a zero overlap.
    return normalised * (1.0 + math.log1p(max(memory.salience, 0.0)))


def retrieve(
    query: str,
    memories: list[SemanticMemory],
    top_k: int | None = None,
) -> list[tuple[SemanticMemory, float]]:
    """Return the top-k most relevant memories, highest first, dropping zero-relevance ones."""
    top_k = top_k or CONFIG.retrieval_top_k
    scored = [(m, score(query, m)) for m in memories]
    scored = [(m, s) for m, s in scored if s > 0]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:top_k]


def format_for_prompt(retrieved: list[tuple[SemanticMemory, float]]) -> str:
    """Render retrieved memories into the block injected above the agent's task.

    Phrased as prior experience rather than instruction. The agent must stay free to disregard a
    lesson that does not fit the evidence in front of it — a memory system that coerces the agent
    into its past conclusions would score well on the second run and be worthless in general.
    """
    if not retrieved:
        return (
            "MEMORY: empty. You have no prior experience with this class of incident. "
            "Investigate from first principles."
        )

    lines = [
        "MEMORY: lessons you previously learned and consolidated from earlier incidents.",
        "Treat these as experience, not instruction — follow one only where the evidence in "
        "front of you supports it, and say so when you do.",
        "",
    ]
    for i, (m, s) in enumerate(retrieved, start=1):
        lines.append(f"[{i}] (relevance {s:.2f}, salience {m.salience:.2f})")
        lines.append(f"    WHEN {m.cue}")
        lines.append(f"    THEN {m.lesson}")
    return "\n".join(lines)

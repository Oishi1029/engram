"""Central configuration.

Every knob the agent has lives here, including the hard safety caps. Those caps are not
performance tuning: an agent looping against a paid model API is the one way this project can
cost real money, so they are treated as a budget control and are enforced in `runner.py`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ[key])
    except (KeyError, ValueError):
        return default


@dataclass(frozen=True)
class Config:
    # --- Google Cloud ---
    project_id: str = _env("GOOGLE_CLOUD_PROJECT", "")
    location: str = _env("GOOGLE_CLOUD_LOCATION", "global")

    # --- Model ---
    # Vertex AI serves the Flash family on the `global` endpoint.
    #
    # The contest mandates "Gemini 3.5 or newer". Note that the Gemini *Pro* line tops out at
    # 3.1 on Vertex AI as of August 2026, so no Pro model satisfies the requirement. The Flash
    # family is the compliant choice, and 3.7 Flash is currently both the newest and — on
    # introductory pricing through 2026-12-31 — cheaper than 3.5 Flash.
    model: str = _env("ENGRAM_MODEL", "gemini-3.7-flash")

    # Cheaper model for high-volume, low-stakes calls (salience scoring, dev iteration).
    small_model: str = _env("ENGRAM_SMALL_MODEL", "gemini-3.5-flash-lite")

    # --- Firestore ---
    database: str = _env("FIRESTORE_DATABASE", "(default)")
    episodic_collection: str = "episodes"
    semantic_collection: str = "semantic_memory"
    runs_collection: str = "runs"

    # --- Transient-failure retry ---
    # Vertex AI returns 429 RESOURCE_EXHAUSTED under burst load. Observed live: several concurrent
    # agents saturated the burst quota and a run died mid-flight, recovering ~15s later. Unhandled,
    # that ends a demo recording or a judge's reproduction on an error that was never a real fault.
    # Retries happen at the transport layer so a single step is retried, not the whole run.
    # 🔴 Tuned deliberately SHORT. An earlier setting (5 attempts, 60s max delay) could stall a
    # single run for 400+ seconds waiting out a burst limit. That is bad twice over: it would
    # overrun a 4-minute demo recording, and in the benchmark it manufactured fake data points —
    # one cold trial recorded 0 tool calls and 1,130 tokens over 484 seconds, having spent its
    # entire life in backoff before hitting the run timeout. Scored naively that reads as "the
    # memoryless agent failed", which inflates the very result being measured.
    # Better to fail fast and visibly than to stall and be silently counted as a failure.
    retry_attempts: int = _env_int("ENGRAM_RETRY_ATTEMPTS", 3)
    retry_initial_delay: float = 1.0
    retry_max_delay: float = 8.0

    # --- 🔴 Hard safety caps. See module docstring. ---
    max_steps: int = _env_int("ENGRAM_MAX_STEPS", 15)
    max_tool_calls: int = _env_int("ENGRAM_MAX_TOOL_CALLS", 25)
    max_tokens_per_run: int = _env_int("ENGRAM_MAX_TOKENS_PER_RUN", 250_000)
    run_timeout_seconds: int = _env_int("ENGRAM_RUN_TIMEOUT_SECONDS", 420)

    # --- Memory behaviour ---
    retrieval_top_k: int = _env_int("ENGRAM_RETRIEVAL_TOP_K", 5)
    consolidation_batch: int = _env_int("ENGRAM_CONSOLIDATION_BATCH", 50)
    salience_decay: float = 0.95
    eviction_threshold: float = 0.15
    max_semantic_memories: int = _env_int("ENGRAM_MAX_SEMANTIC", 200)

    def require_project(self) -> str:
        if not self.project_id:
            raise RuntimeError(
                "GOOGLE_CLOUD_PROJECT is not set. Set it to your Google Cloud project ID."
            )
        return self.project_id


CONFIG = Config()

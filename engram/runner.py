"""Run one investigation end to end, and measure it.

The measurement is not incidental — it *is* the submission's argument. "The agent seems smarter
with memory" is unfalsifiable; "the warm run resolved an unseen incident in 5 tool calls instead
of 11, and got the root cause right where the cold run's first hypothesis was wrong" is a claim a
judge can check against the terminal on screen.

Metrics captured per run:
    tool_calls        - the headline number. Directly visible in the live demo.
    wall_seconds      - end-to-end latency.
    prompt/output/total tokens - cost, from the model's own usage metadata.
    correct           - did it identify the right root-cause service?
    wasted_steps      - tool calls spent on the incident's red herring, i.e. the dead end that
                        memory is supposed to teach it to skip.
    memories_used     - which consolidated lessons were in context.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from google.adk.runners import InMemoryRunner
from google.genai import types

from engram.agent import build_agent
from engram.config import CONFIG
from engram.env.fixtures import Incident, get_incident
from engram.env.tools import ToolContext, build_tools
from engram.memory import retrieval
from engram.memory.store import Episode, MemoryStore

APP_NAME = "engram"


def _task_prompt(incident: Incident) -> str:
    return (
        f"INCIDENT {incident.incident_id}\n"
        f"Reported at: {incident.reported_at}\n"
        f"Affected service: {incident.affected_service}\n"
        f"Report: {incident.title}\n\n"
        f"Investigate and propose a remediation."
    )


def _situation_query(incident: Incident) -> str:
    """What we match memories against.

    Deliberately the *symptom description only* — the title and the affected service. It must not
    include anything the agent has not yet discovered, or retrieval would be leaking the answer
    and the second run's advantage would be an artefact of this function rather than of memory.
    """
    return f"{incident.title} affected service {incident.affected_service}"


async def run_incident(
    incident_id: str,
    store: MemoryStore | None = None,
    use_memory: bool = True,
    verbose: bool = True,
) -> dict[str, Any]:
    incident = get_incident(incident_id)
    store = store or MemoryStore()
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    started = time.time()

    # --- retrieve -------------------------------------------------------------
    retrieved: list = []
    if use_memory:
        retrieved = retrieval.retrieve(_situation_query(incident), store.all_semantic())
        if retrieved:
            store.touch_retrieved([m.memory_id for m, _ in retrieved])
    memory_block = retrieval.format_for_prompt(retrieved)

    if verbose:
        print(f"\n{'=' * 78}")
        print(f"RUN {run_id}  |  incident {incident.incident_id}  |  memory={'ON' if use_memory else 'OFF'}")
        print(f"{'=' * 78}")
        print(memory_block)
        print("-" * 78)

    # --- run ------------------------------------------------------------------
    ctx = ToolContext(incident=incident)
    agent = build_agent(build_tools(ctx), memory_block)
    adk = InMemoryRunner(agent=agent, app_name=APP_NAME)
    session = await adk.session_service.create_session(app_name=APP_NAME, user_id="oncall")

    step = 0
    prompt_tokens = output_tokens = 0
    pending_thought = ""
    timed_out = False

    try:
        async for event in adk.run_async(
            user_id="oncall",
            session_id=session.id,
            new_message=types.Content(
                role="user", parts=[types.Part(text=_task_prompt(incident))]
            ),
        ):
            usage = getattr(event, "usage_metadata", None)
            if usage:
                prompt_tokens += getattr(usage, "prompt_token_count", 0) or 0
                output_tokens += getattr(usage, "candidates_token_count", 0) or 0

            content = getattr(event, "content", None)
            for part in getattr(content, "parts", None) or []:
                if getattr(part, "text", None):
                    pending_thought = part.text.strip()
                    if verbose and pending_thought:
                        print(f"  · {pending_thought}")

                call = getattr(part, "function_call", None)
                if call is not None:
                    step += 1
                    args = dict(call.args or {})
                    if verbose:
                        print(f"  [{step}] {call.name}({', '.join(f'{k}={v!r}' for k, v in args.items())})")
                    store.write_episode(
                        Episode(
                            run_id=run_id,
                            incident_id=incident.incident_id,
                            step=step,
                            thought=pending_thought or "(no stated rationale)",
                            tool=call.name,
                            tool_args=args,
                            observation_summary=_summarise_observation(ctx, call.name),
                        )
                    )
                    pending_thought = ""

            # 🔴 Hard caps. See config.py — these are a cost control, not tuning.
            if step >= CONFIG.max_tool_calls:
                if verbose:
                    print(f"  !! tool-call cap ({CONFIG.max_tool_calls}) reached, stopping")
                break
            if (prompt_tokens + output_tokens) >= CONFIG.max_tokens_per_run:
                if verbose:
                    print("  !! token cap reached, stopping")
                break
            if time.time() - started > CONFIG.run_timeout_seconds:
                timed_out = True
                if verbose:
                    print("  !! run timeout reached, stopping")
                break
    finally:
        await adk.close()

    # --- grade ----------------------------------------------------------------
    elapsed = time.time() - started
    proposal = ctx.proposal or {}
    proposed_service = (proposal.get("root_cause_service") or "").strip()
    correct = proposed_service == incident.root_cause_service

    wasted = sum(
        1 for c in ctx.call_log if c["args"].get("service") == incident.red_herring_service
    )

    record = {
        "run_id": run_id,
        "incident_id": incident.incident_id,
        "started_at": started,
        "memory_enabled": use_memory,
        "memories_used": [m.memory_id for m, _ in retrieved],
        "memories_used_count": len(retrieved),
        "tool_calls": ctx.tool_call_count,
        "wall_seconds": round(elapsed, 2),
        "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "total_tokens": prompt_tokens + output_tokens,
        "services_investigated": ctx.services_investigated,
        "wasted_steps_on_red_herring": wasted,
        "proposed_root_cause_service": proposed_service,
        "expected_root_cause_service": incident.root_cause_service,
        "correct": correct,
        "proposed_action": proposal.get("action", ""),
        "proposed_reasoning": proposal.get("root_cause", ""),
        "timed_out": timed_out,
    }
    store.write_run(record)

    # A lesson that was in context during a correct run earns salience. This is the feedback loop
    # that makes retrieval get better rather than merely staying warm.
    if correct and retrieved:
        store.reward([m.memory_id for m, _ in retrieved])

    if verbose:
        print("-" * 78)
        print(
            f"RESULT  {'CORRECT' if correct else 'WRONG'}  "
            f"| proposed {proposed_service or '(nothing)'} "
            f"| expected {incident.root_cause_service}"
        )
        print(
            f"        {ctx.tool_call_count} tool calls "
            f"| {elapsed:.1f}s "
            f"| {prompt_tokens + output_tokens} tokens "
            f"| {wasted} spent on the red herring ({incident.red_herring_service})"
        )
    return record


def _summarise_observation(ctx: ToolContext, tool_name: str) -> str:
    """A compact description of what the last call returned, for the episodic record."""
    if not ctx.call_log:
        return ""
    last = ctx.call_log[-1]
    keys = last.get("result_keys")
    target = last["args"].get("service", "")
    return f"{tool_name}({target}) -> {keys}"


def run_incident_sync(incident_id: str, **kwargs: Any) -> dict[str, Any]:
    return asyncio.run(run_incident(incident_id, **kwargs))

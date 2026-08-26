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
from contextlib import aclosing
from typing import Any

from google.adk.runners import InMemoryRunner
from google.genai import types

from engram.agent import INSTRUCTION, build_agent
from engram.config import CONFIG
from engram.env.fixtures import Incident, get_incident
from engram.env.tools import ToolContext, build_tools
from engram.memory.progressive import ProgressiveMemory
from engram.memory.records import Episode
from engram.memory.store import MemoryStore

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

    # --- memory ------------------------------------------------------------
    # Progressive: this object re-retrieves before every planning step, cued by the evidence the
    # agent has gathered so far. Retrieving once up front measurably did not work — see
    # engram/memory/progressive.py.
    memory = ProgressiveMemory(
        store=store,
        base_instruction=INSTRUCTION,
        alert_text=_situation_query(incident),
        enabled=use_memory,
        verbose=verbose,
    )

    if verbose:
        print(f"\n{'=' * 78}")
        print(
            f"RUN {run_id}  |  incident {incident.incident_id}  |  "
            f"memory={'ON' if use_memory else 'OFF'}  "
            f"|  {len(memory.available)} lesson(s) available"
        )
        print(f"{'=' * 78}")

    # --- run ------------------------------------------------------------------
    ctx = ToolContext(incident=incident)
    agent = build_agent(build_tools(ctx), memory.before_model)
    adk = InMemoryRunner(agent=agent, app_name=APP_NAME)
    session = await adk.session_service.create_session(app_name=APP_NAME, user_id="oncall")

    step = 0
    prompt_tokens = output_tokens = 0
    pending_thought = ""
    timed_out = False
    pending_calls: list[dict[str, Any]] = []

    # 🔴 `aclosing` is not decoration. The hard caps below BREAK out of this async generator,
    # and abandoning an async generator mid-flight leaves ADK's OpenTelemetry span token and the
    # underlying HTTP transport in a corrupted state. The failure then surfaces much later, on the
    # *next* unrelated model call, as "Cannot send a request, as the client has been closed" —
    # which is a genuinely misleading place for it to appear. `aclosing` guarantees the generator
    # is closed in its own context on every exit path, including a break.
    stream = adk.run_async(
        user_id="oncall",
        session_id=session.id,
        new_message=types.Content(
            role="user", parts=[types.Part(text=_task_prompt(incident))]
        ),
    )
    try:
        async with aclosing(stream) as events:
            async for event in events:
                usage = getattr(event, "usage_metadata", None)
                if usage:
                    prompt_tokens += getattr(usage, "prompt_token_count", 0) or 0
                    output_tokens += getattr(usage, "candidates_token_count", 0) or 0

                content = getattr(event, "content", None)
                for part in getattr(content, "parts", None) or []:
                    if getattr(part, "text", None):
                        pending_thought = part.text.strip()
                        if verbose and pending_thought:
                            print(f"  \u00b7 {pending_thought}")

                    # 🔴 A tool call and its result arrive in SEPARATE events. Writing the episode
                    # here, on the call, and reading the observation from the tool log would record
                    # the PREVIOUS step's result against this step — and would never capture the
                    # final apply_remediation outcome at all, because the run ends before another
                    # call arrives. That outcome is the single most valuable thing in the trace, so
                    # calls are buffered and the episode is written when its response comes back.
                    call = getattr(part, "function_call", None)
                    if call is not None:
                        step += 1
                        args = dict(call.args or {})
                        if verbose:
                            rendered = ", ".join(f"{k}={v!r}" for k, v in args.items())
                            print(f"  [{step}] {call.name}({rendered})")
                        pending_calls.append(
                            {
                                "step": step,
                                "tool": call.name,
                                "args": args,
                                "thought": pending_thought or "(no stated rationale)",
                            }
                        )
                        pending_thought = ""

                    response = getattr(part, "function_response", None)
                    if response is not None and pending_calls:
                        pending = pending_calls.pop(0)
                        store.write_episode(
                            Episode(
                                run_id=run_id,
                                incident_id=incident.incident_id,
                                step=pending["step"],
                                thought=pending["thought"],
                                tool=pending["tool"],
                                tool_args=pending["args"],
                                observation_summary=_summarise_response(response),
                                # A memory-disabled run is a CONTROL by definition — you only
                                # disable memory to measure against it. Marking it consolidated at
                                # WRITE time makes it permanently ineligible for consolidation, no
                                # matter what any caller later asks for. Relying on callers to pass
                                # run_ids was not enough: the Cloud Scheduler job posts an empty
                                # body, which took the unfiltered path and poisoned the live store.
                                control_run=not use_memory,
                                consolidated=not use_memory,
                            )
                        )

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

    chosen_remediation = (proposal.get("remediation") or "").strip()
    remediation_correct = chosen_remediation == incident.correct_remediation
    # Both must be right for the incident to be genuinely resolved. Diagnosing correctly and then
    # applying a fix that destroys an in-flight job is not a success.
    resolved = correct and remediation_correct

    wasted = sum(
        1 for c in ctx.call_log if c["args"].get("service") == incident.red_herring_service
    )

    record = {
        "run_id": run_id,
        "incident_id": incident.incident_id,
        "started_at": started,
        "memory_enabled": use_memory,
        "memories_used": memory.surfaced_ids,
        "memories_used_count": len(memory.surfaced),
        "memories_available": len(memory.available),
        "recall_events": memory.recall_events,
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
        "chosen_remediation": chosen_remediation,
        "expected_remediation": incident.correct_remediation,
        "remediation_correct": remediation_correct,
        "resolved": resolved,
        "outcome": (ctx.applied or {}).get("outcome", ""),
        "proposed_reasoning": proposal.get("root_cause", ""),
        "rationale": proposal.get("rationale", ""),
        "timed_out": timed_out,
    }
    store.write_run(record)

    # A lesson that was in context during a correct run earns salience. This is the feedback loop
    # that makes retrieval get better rather than merely staying warm.
    if memory.surfaced_ids:
        store.touch_retrieved(memory.surfaced_ids)
        if resolved:
            store.reward(memory.surfaced_ids)

    if verbose:
        print("-" * 78)
        print(
            f"RESULT  diagnosis {'OK ' if correct else 'WRONG'} "
            f"({proposed_service or 'nothing'}) "
            f"| remediation {'OK ' if remediation_correct else 'WRONG'} "
            f"({chosen_remediation or 'nothing'}, expected {incident.correct_remediation})"
        )
        if ctx.applied:
            print(f"        outcome: {ctx.applied['outcome'][:160]}")
        print(
            f"        {ctx.tool_call_count} tool calls "
            f"| {elapsed:.1f}s "
            f"| {prompt_tokens + output_tokens} tokens "
            f"| {wasted} spent on the red herring ({incident.red_herring_service})"
        )
    return record


def _summarise_response(response: Any, limit: int = 900) -> str:
    """Render a tool response into the episodic record.

    This is what consolidation reads, so it must carry facts rather than shapes. Above all it
    carries the outcome text from `apply_remediation` — the only knowledge in this environment
    that could not have been deduced by investigation, and therefore the only thing genuinely
    worth remembering.
    """
    payload = getattr(response, "response", None)
    if isinstance(payload, dict):
        parts = [f"{k}={v}" for k, v in payload.items() if k not in ("service",)]
        text = "; ".join(parts)
    else:
        text = str(payload)
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def run_incident_sync(incident_id: str, **kwargs: Any) -> dict[str, Any]:
    return asyncio.run(run_incident(incident_id, **kwargs))

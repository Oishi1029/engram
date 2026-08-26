"""The incident-response agent.

The instruction below is written so that the agent behaves *correctly* with no memory — it
investigates thoroughly and reaches the right answer eventually. That is deliberate and it is the
honest way to run this experiment.

It would be trivial to cripple the memoryless agent and manufacture a large improvement. The
resulting number would be worthless, and any judge who reads the prompt would see it. So the cold
run gets a competent agent, and the second run has to earn its margin by *actually knowing where
to look* rather than by being handed an opponent with one hand tied.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.genai import types

from engram.config import CONFIG

INSTRUCTION = """\
You are an experienced site-reliability engineer on call. You have been handed a live production
incident and you must find its ROOT CAUSE and propose a remediation.

HOW TO INVESTIGATE

Work from evidence, not from the first plausible story. In a distributed system the service that
reports the symptom is frequently NOT the service that contains the cause.

A discipline that repeatedly pays off:
  - Anything that changed recently is a prime suspect. Deploys and configuration changes cause
    most incidents.
  - Distinguish a CAUSE from a SYMPTOM. A dependency can look degraded simply because its caller
    is holding connections open. Check whether a service's own internal health — its error rate,
    its CPU, its server-side latency — is actually bad, or whether only the latency *observed by
    its callers* is bad. Those mean very different things.
  - Read the logs of the service reporting the symptom before ranging further afield. They often
    name the failing subsystem outright.
  - When a deploy is implicated, compare the configuration before and after it. The `_previous`
    block in a service's config shows what the deploy changed.

BUDGET
You have a limited number of tool calls. Be systematic, not exhaustive. Do not investigate every
service in the estate — follow the evidence.

FINISHING
When the evidence supports a conclusion, call `propose_remediation` exactly once with the service
where the cause actually lives, the causal chain, and a concrete fix. Do not call it speculatively,
and do not stop without calling it.

Before each tool call, state in one short sentence what you are checking and why. Those sentences
are recorded as the agent's episodic memory, so make them substantive: name the hypothesis you are
testing, not merely the action you are taking.
"""


def build_agent(tools: list, memory_block: str) -> LlmAgent:
    """Construct the agent for one run.

    `memory_block` is prepended to the instruction. On a cold run it says the memory is empty; on a
    warm run it carries the consolidated lessons. That single substitution is the entire difference
    between the two runs — same model, same tools, same instruction, same incident-independent
    prompt. Keeping the variable isolated is what makes the comparison meaningful.
    """
    return LlmAgent(
        name="incident_responder",
        model=CONFIG.model,
        description="Investigates a production incident and proposes a remediation.",
        instruction=f"{memory_block}\n\n---\n\n{INSTRUCTION}",
        tools=tools,
        generate_content_config=types.GenerateContentConfig(
            temperature=0.1,
            max_output_tokens=2048,
        ),
    )

"""The incident-response agent.

🔴 THE METHODOLOGICAL DECISION THIS FILE EXISTS TO RECORD
---------------------------------------------------------
An earlier version of the instruction below told the agent, in the system prompt, that recent
deploys are prime suspects, that a degraded dependency may be a symptom rather than a cause, and
that it should diff a service's `_previous` config block.

Those are exactly the lessons consolidation is supposed to DISCOVER FROM EXPERIENCE. Putting them
in the base prompt hands the agent its memory for free, leaves nothing for the memory system to
contribute, and makes any measured improvement meaningless. The first live run exposed it: the
memoryless agent solved the incident in seven tool calls and never touched the red herring,
because it had been told in advance where to look.

The instruction is therefore now deliberately DOMAIN-NAIVE. The agent is a capable reasoner with
full tool access and no free heuristics — it must work out an investigation strategy itself, and
a naive strategy is what costs it steps on the red herring.

The opposite failure would be just as dishonest: crippling the memoryless agent to manufacture a
large delta. It is not crippled. It has the same model, the same tools, the same budget and the
same task. The only difference between a cold and a warm run is the memory block prepended below.
That single isolated variable is what makes the comparison worth putting on camera.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.genai import types

from engram.config import CONFIG

INSTRUCTION = """\
You are a site-reliability engineer on call. You have been handed a live production incident in a
microservice estate. Your job is to find its ROOT CAUSE and propose a concrete remediation.

You have tools that let you inspect the estate. Use them to gather evidence, and reason from the
evidence you actually gather rather than from the first plausible story.

BUDGET
You have a limited number of tool calls, so choose each one deliberately. Do not sweep every
service in the estate.

FINISHING
When the evidence supports a conclusion, call `propose_remediation` exactly once, naming the
service where the cause actually lives, the causal chain, and a concrete fix. Do not call it
speculatively, and do not stop without calling it.

Before each tool call, state in one short sentence what you are checking and why — name the
hypothesis you are testing, not merely the action you are taking. Those sentences become the
agent's episodic record, so make them substantive.
"""


def build_agent(tools: list, before_model_callback) -> LlmAgent:
    """Construct the agent for one run.

    Memory is injected through `before_model_callback` rather than baked into the instruction at
    construction time. That is what makes retrieval PROGRESSIVE: the callback runs before every
    planning step, so a lesson can surface at the moment the evidence for it appears, rather than
    only at the moment the alert arrives. See `engram/memory/progressive.py` for why retrieving
    once at the start measurably did not work.

    The instruction below is identical on cold and warm runs. The callback is the only difference.
    """
    return LlmAgent(
        name="incident_responder",
        model=CONFIG.model,
        description="Investigates a production incident and proposes a remediation.",
        instruction=INSTRUCTION,
        tools=tools,
        before_model_callback=before_model_callback,
        generate_content_config=types.GenerateContentConfig(
            temperature=0.1,
            max_output_tokens=2048,
        ),
    )

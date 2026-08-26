"""The agent's tools — a read-only investigation surface over the synthetic estate, plus one
terminal action.

Design notes that matter for judging:

* Every tool returns a *dict*, never prose. ADK serialises these into the model context, and
  structured returns keep the agent's reasoning auditable in the terminal during the live demo.
* Tool calls are counted. Step count is the headline metric the second run has to beat, so the
  counter lives here rather than being inferred from a transcript afterwards.
* `apply_remediation` is the only tool with side effects, and it terminates the run. An agent that
  never applies anything fails the task — this is a workflow that takes action, not a chatbot that
  describes one.
* Crucially, `apply_remediation` RETURNS WHAT HAPPENED, including operational side effects that no
  amount of investigation could have predicted. Rolling back the offending deploy relieves the
  symptom and destroys an in-flight job; reducing the client pool in place relieves it just as well
  and lets the job finish. Nothing in the metrics, logs, configs or topology distinguishes them.
  That outcome text is the raw material consolidation turns into a durable operational lesson, and
  it is the reason memory can beat a capable model here rather than merely saving it a few steps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from engram.env.fixtures import SERVICES, Incident


@dataclass
class ToolContext:
    """Per-run tool state: which incident is live, and what the agent has done so far."""

    incident: Incident
    call_log: list[dict[str, Any]] = field(default_factory=list)
    proposal: dict[str, Any] | None = None
    applied: dict[str, Any] | None = None

    def _record(self, name: str, args: dict[str, Any], result: Any) -> None:
        self.call_log.append(
            {"tool": name, "args": args, "summary": _summarise(result)}
        )

    @property
    def tool_call_count(self) -> int:
        return len(self.call_log)

    @property
    def services_investigated(self) -> list[str]:
        seen: list[str] = []
        for call in self.call_log:
            svc = call["args"].get("service")
            if svc and svc not in seen:
                seen.append(svc)
        return seen


def _summarise(result: Any, limit: int = 600) -> str:
    """A readable digest of a tool result, for the episodic record.

    🔴 This used to return only the result's KEYS. That was a mistake with consequences: episodes
    are the raw material consolidation reasons over, and "get_service_metrics(redis-session) ->
    ['cpu_pct', 'connections_active', ...]" contains no facts to generalise from. Worse, the
    outcome text returned by `apply_remediation` — the one thing in this environment that cannot
    be deduced and therefore the only thing genuinely worth remembering — was being discarded
    entirely before it ever reached the consolidation model.
    """
    if isinstance(result, dict):
        parts = []
        for k, v in result.items():
            if k in ("service", "applied"):
                continue
            text = ", ".join(str(x) for x in v) if isinstance(v, list) else str(v)
            parts.append(f"{k}={text}")
        out = "; ".join(parts)
    elif isinstance(result, list):
        out = "; ".join(str(x) for x in result)
    else:
        out = str(result)
    return out if len(out) <= limit else out[: limit - 1] + "\u2026"


def build_tools(ctx: ToolContext) -> list:
    """Return plain functions bound to `ctx`.

    ADK derives each tool's schema from its signature and docstring, so the docstrings below are
    part of the prompt, not just documentation. They are written for the model to read.
    """

    def list_services() -> dict:
        """List every service in the estate with its tier, owner and direct dependencies.

        Use this first to understand the topology before investigating anything specific.
        """
        result = {
            "services": {
                name: {
                    "tier": meta["tier"],
                    "owner": meta["owner"],
                    "depends_on": meta["depends_on"],
                }
                for name, meta in SERVICES.items()
            }
        }
        ctx._record("list_services", {}, result)
        return result

    def get_service_metrics(service: str) -> dict:
        """Get current metrics for one service: latency percentiles, error rate, CPU, memory,
        and — for datastores — connection pool utilisation.

        Args:
            service: The exact service name, e.g. "checkout-api".
        """
        args = {"service": service}
        metrics = ctx.incident.metrics.get(service)
        if metrics is None:
            result = {
                "service": service,
                "error": "No metrics recorded for this service during the incident window.",
                "known_services": sorted(ctx.incident.metrics),
            }
        else:
            result = {"service": service, **metrics}
        ctx._record("get_service_metrics", args, result)
        return result

    def search_logs(service: str) -> dict:
        """Retrieve log lines emitted by one service during the incident window.

        Args:
            service: The exact service name, e.g. "checkout-api".
        """
        args = {"service": service}
        lines = ctx.incident.logs.get(service)
        if lines is None:
            result = {
                "service": service,
                "lines": [],
                "note": "No log lines captured for this service in the incident window.",
            }
        else:
            result = {"service": service, "lines": list(lines), "count": len(lines)}
        ctx._record("search_logs", args, result)
        return result

    def get_recent_deploys(service: str) -> dict:
        """List recent deployments for one service, newest first, with what each change did.

        Args:
            service: The exact service name, e.g. "checkout-api".
        """
        args = {"service": service}
        deploys = ctx.incident.deploys.get(service, [])
        result = {"service": service, "deploys": list(deploys), "count": len(deploys)}
        ctx._record("get_recent_deploys", args, result)
        return result

    def get_service_config(service: str) -> dict:
        """Get the current runtime configuration for one service, including a `_previous` block
        showing the values before the most recent deploy where one exists.

        Comparing current against `_previous` is how you detect a change that a deploy introduced.

        Args:
            service: The exact service name, e.g. "checkout-api".
        """
        args = {"service": service}
        cfg = ctx.incident.configs.get(service)
        if cfg is None:
            result = {"service": service, "error": "No configuration recorded for this service."}
        else:
            result = {"service": service, "config": dict(cfg)}
        ctx._record("get_service_config", args, result)
        return result

    def get_dependencies(service: str) -> dict:
        """Show what one service depends on, and what depends on it.

        Useful for telling a cause apart from a symptom: a slow dependency degrades its callers,
        so callers looking slow does not mean the cause is in the caller.

        Args:
            service: The exact service name, e.g. "checkout-api".
        """
        args = {"service": service}
        meta = SERVICES.get(service)
        if meta is None:
            result = {"service": service, "error": "Unknown service.", "known": sorted(SERVICES)}
        else:
            dependents = [n for n, m in SERVICES.items() if service in m["depends_on"]]
            result = {
                "service": service,
                "depends_on": meta["depends_on"],
                "depended_on_by": dependents,
            }
        ctx._record("get_dependencies", args, result)
        return result

    def apply_remediation(
        root_cause_service: str, root_cause: str, remediation: str, rationale: str
    ) -> dict:
        """Resolve the incident by APPLYING a remediation. This ENDS the run — call it exactly
        once, and only when the evidence supports your diagnosis.

        This performs a real change on the estate and returns what actually happened, including
        any operational side effects.

        Args:
            root_cause_service: The service where the root cause actually lives — not the service
                that reported symptoms, unless they are the same.
            root_cause: One or two sentences explaining the causal chain.
            remediation: Exactly one of:
                "rollback_deploy"             - roll the offending deploy back to its previous
                                                version, reverting all of its changes.
                "reduce_client_pool_in_place" - lower the offending service's connection-pool size
                                                at runtime, without a restart or a rollback.
                "raise_datastore_capacity"    - raise the shared datastore's connection limit.
            rationale: Why you chose this remediation over the alternatives.
        """
        choice = (remediation or "").strip()
        args = {
            "root_cause_service": root_cause_service,
            "root_cause": root_cause,
            "remediation": choice,
            "rationale": rationale,
        }
        outcome = ctx.incident.remediation_outcomes.get(choice)
        if outcome is None:
            result = {
                "applied": False,
                "error": f"Unknown remediation {choice!r}.",
                "valid_options": sorted(ctx.incident.remediation_outcomes),
            }
            ctx._record("apply_remediation", args, result)
            return result

        ctx.proposal = dict(args)
        ctx.applied = {"remediation": choice, "outcome": outcome}
        result = {"applied": True, "remediation": choice, "outcome": outcome}
        ctx._record("apply_remediation", args, result)
        return result

    return [
        list_services,
        get_service_metrics,
        search_logs,
        get_recent_deploys,
        get_service_config,
        get_dependencies,
        apply_remediation,
    ]

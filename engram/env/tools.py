"""The agent's tools — a read-only investigation surface over the synthetic estate, plus one
terminal action.

Design notes that matter for judging:

* Every tool returns a *dict*, never prose. ADK serialises these into the model context, and
  structured returns keep the agent's reasoning auditable in the terminal during the live demo.
* Tool calls are counted. Step count is the headline metric the second run has to beat, so the
  counter lives here rather than being inferred from a transcript afterwards.
* `propose_remediation` is the only tool with side effects, and it terminates the run. An agent
  that never proposes anything fails the task; this is what makes it a Taskmaster workflow rather
  than a chatbot.
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

    def _record(self, name: str, args: dict[str, Any], result: Any) -> None:
        self.call_log.append({"tool": name, "args": args, "result_keys": _shape(result)})

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


def _shape(result: Any) -> Any:
    if isinstance(result, dict):
        return sorted(result)
    if isinstance(result, list):
        return f"list[{len(result)}]"
    return type(result).__name__


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

    def propose_remediation(root_cause_service: str, root_cause: str, action: str) -> dict:
        """Conclude the investigation by proposing a fix. This ENDS the run — call it exactly once,
        and only when the evidence supports it.

        Args:
            root_cause_service: The service where the root cause actually lives (not the service
                that reported symptoms, unless they are the same).
            root_cause: One or two sentences explaining the causal chain.
            action: The concrete remediation to apply.
        """
        args = {
            "root_cause_service": root_cause_service,
            "root_cause": root_cause,
            "action": action,
        }
        ctx.proposal = dict(args)
        result = {"accepted": True, "recorded": args}
        ctx._record("propose_remediation", args, result)
        return result

    return [
        list_services,
        get_service_metrics,
        search_logs,
        get_recent_deploys,
        get_service_config,
        get_dependencies,
        propose_remediation,
    ]

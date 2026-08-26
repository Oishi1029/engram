"""A synthetic production estate, and the two incidents the agent investigates.

All data here is fabricated. No real system, customer or person is represented — which is both
an ethical baseline and a contest requirement, since this data appears on screen in the demo video.

THE DESIGN THAT MAKES THE DEMO WORK
-----------------------------------
The two incidents are *structurally identical and superficially unrelated*:

    INC-001  checkout-api  --(worker concurrency raised by a deploy)-->  redis-session pool exhausted
    INC-002  search-api    --(worker concurrency raised by a deploy)-->  postgres-primary pool exhausted

Different services, different datastores, different symptoms, different red herrings. The only
thing they share is the causal shape: *a deploy raised concurrency without raising the connection
pool to match, and the datastore's own elevated metrics are a symptom rather than the cause.*

That is deliberate. Replaying one incident twice would demonstrate a cache. Generalising from
INC-001 to INC-002 — an incident the agent has never seen — is the actual claim this project makes,
and it is the thing the second run has to prove on camera.

Each incident also carries a RED HERRING: a downstream service whose metrics look alarming and
which a memoryless agent reliably investigates first. That is what the step-count delta measures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# --------------------------------------------------------------------------------------
# Service catalogue
# --------------------------------------------------------------------------------------

SERVICES: dict[str, dict[str, Any]] = {
    "web-frontend": {
        "tier": "edge",
        "language": "TypeScript",
        "owner": "team-storefront",
        "depends_on": ["checkout-api", "search-api", "user-profile-api"],
    },
    "checkout-api": {
        "tier": "application",
        "language": "Python",
        "owner": "team-payments",
        "depends_on": ["payments-api", "inventory-api", "redis-session"],
    },
    "payments-api": {
        "tier": "application",
        "language": "Go",
        "owner": "team-payments",
        "depends_on": ["postgres-primary"],
    },
    "inventory-api": {
        "tier": "application",
        "language": "Java",
        "owner": "team-supply",
        "depends_on": ["postgres-primary"],
    },
    "search-api": {
        "tier": "application",
        "language": "Python",
        "owner": "team-discovery",
        "depends_on": ["postgres-primary", "opensearch-cluster"],
    },
    "user-profile-api": {
        "tier": "application",
        "language": "Go",
        "owner": "team-identity",
        "depends_on": ["postgres-primary", "redis-session"],
    },
    "redis-session": {
        "tier": "datastore",
        "language": "-",
        "owner": "team-platform",
        "depends_on": [],
    },
    "postgres-primary": {
        "tier": "datastore",
        "language": "-",
        "owner": "team-platform",
        "depends_on": [],
    },
    "opensearch-cluster": {
        "tier": "datastore",
        "language": "-",
        "owner": "team-platform",
        "depends_on": [],
    },
    "notification-worker": {
        "tier": "async",
        "language": "Python",
        "owner": "team-comms",
        "depends_on": ["postgres-primary"],
    },
}


@dataclass(frozen=True)
class Incident:
    """One synthetic outage, with everything the tools can surface about it."""

    incident_id: str
    title: str
    reported_at: str
    affected_service: str

    # The answer. Used only for grading a completed run — never exposed to the agent.
    root_cause_service: str
    root_cause_summary: str
    correct_remediation_keywords: list[str]

    # The service whose metrics look alarming but which is a symptom, not a cause.
    red_herring_service: str

    metrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    logs: dict[str, list[str]] = field(default_factory=dict)
    deploys: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    configs: dict[str, dict[str, Any]] = field(default_factory=dict)


# --------------------------------------------------------------------------------------
# INC-001 — the cold run. checkout-api / redis-session.
# --------------------------------------------------------------------------------------

INC_001 = Incident(
    incident_id="INC-001",
    title="Checkout p99 latency spiked to 8.2s; customers reporting timeouts at payment step",
    reported_at="2026-08-26T14:02:00Z",
    affected_service="checkout-api",
    root_cause_service="redis-session",
    root_cause_summary=(
        "The 13:58 deploy of checkout-api raised worker_concurrency from 20 to 60 but left "
        "redis.pool_size at 10. Workers now queue for Redis session connections, so p99 latency "
        "collapses. payments-api looks slow only because checkout-api holds its calls open."
    ),
    correct_remediation_keywords=["pool_size", "redis", "concurrency", "rollback", "deploy"],
    red_herring_service="payments-api",
    metrics={
        "checkout-api": {
            "p50_latency_ms": 240, "p99_latency_ms": 8210, "error_rate_pct": 4.1,
            "cpu_pct": 34, "memory_pct": 51, "rps": 820,
            "note": "p99 stepped up sharply at 13:59, one minute after the last deploy.",
        },
        "payments-api": {
            "p50_latency_ms": 180, "p99_latency_ms": 5900, "error_rate_pct": 0.2,
            "cpu_pct": 22, "memory_pct": 44, "rps": 780,
            "note": "p99 elevated but error rate is normal and CPU is low.",
        },
        "redis-session": {
            "p50_latency_ms": 1, "p99_latency_ms": 3, "error_rate_pct": 0.0,
            "cpu_pct": 18, "memory_pct": 40,
            "connections_active": 10, "connections_max": 10,
            "connection_wait_ms_p99": 7400,
            "note": "Server-side latency is healthy. Active connections are pinned at the maximum.",
        },
        "inventory-api": {
            "p50_latency_ms": 95, "p99_latency_ms": 310, "error_rate_pct": 0.1,
            "cpu_pct": 28, "memory_pct": 39, "rps": 410, "note": "Nominal.",
        },
        "postgres-primary": {
            "p50_latency_ms": 4, "p99_latency_ms": 22, "error_rate_pct": 0.0,
            "cpu_pct": 31, "memory_pct": 55,
            "connections_active": 84, "connections_max": 300, "note": "Nominal.",
        },
    },
    logs={
        "checkout-api": [
            "2026-08-26T13:59:14Z WARN  redis.pool  connection pool timeout after 5000ms (pool_size=10, waiters=47)",
            "2026-08-26T14:00:02Z WARN  redis.pool  connection pool timeout after 5000ms (pool_size=10, waiters=51)",
            "2026-08-26T14:01:37Z ERROR checkout    session lookup failed: pool exhausted; falling back to slow path",
            "2026-08-26T14:02:11Z WARN  upstream    payments-api call exceeded 5s budget (held open awaiting session)",
        ],
        "payments-api": [
            "2026-08-26T14:01:40Z INFO  http  POST /v1/charge 200 in 174ms",
            "2026-08-26T14:02:03Z INFO  http  POST /v1/charge 200 in 191ms",
            "2026-08-26T14:02:29Z INFO  http  POST /v1/charge 200 in 168ms",
        ],
        "redis-session": [
            "2026-08-26T13:59:00Z INFO  clients  connected_clients=10 maxclients=10",
            "2026-08-26T14:02:00Z WARN  clients  max number of clients reached",
        ],
    },
    deploys={
        "checkout-api": [
            {
                "deployed_at": "2026-08-26T13:58:00Z",
                "version": "v2026.08.26-3",
                "author": "team-payments",
                "change_summary": "Raise worker_concurrency 20 -> 60 to absorb flash-sale traffic",
            },
            {
                "deployed_at": "2026-08-24T09:12:00Z",
                "version": "v2026.08.24-1",
                "author": "team-payments",
                "change_summary": "Add idempotency key to charge endpoint",
            },
        ],
        "payments-api": [
            {
                "deployed_at": "2026-08-21T11:40:00Z",
                "version": "v2026.08.21-2",
                "author": "team-payments",
                "change_summary": "Upgrade Go runtime to 1.25",
            }
        ],
        "redis-session": [],
    },
    configs={
        "checkout-api": {
            "worker_concurrency": 60,
            "redis.pool_size": 10,
            "redis.pool_timeout_ms": 5000,
            "payments.timeout_ms": 5000,
            "_previous": {"worker_concurrency": 20, "redis.pool_size": 10},
        },
        "payments-api": {"worker_concurrency": 40, "db.pool_size": 40},
        "redis-session": {"maxclients": 10},
    },
)


# --------------------------------------------------------------------------------------
# INC-002 — the warm run. search-api / postgres-primary.
#
# Same causal shape as INC-001. Everything on the surface differs: the service, the datastore,
# the red herring, and the fact that postgres CPU IS genuinely elevated, which makes "slow query"
# the obvious and wrong first hypothesis.
# --------------------------------------------------------------------------------------

INC_002 = Incident(
    incident_id="INC-002",
    title="Search p99 latency spiked to 5.4s with elevated 5xx; catalogue browse degraded",
    reported_at="2026-08-27T09:14:00Z",
    affected_service="search-api",
    root_cause_service="postgres-primary",
    root_cause_summary=(
        "The 09:09 deploy of search-api raised worker_concurrency from 15 to 50 but left "
        "db.pool_size at 12. Workers queue for Postgres connections. postgres-primary CPU is "
        "elevated as a consequence of the extra connection churn, not as the cause — the slow "
        "query hypothesis is a dead end."
    ),
    correct_remediation_keywords=["pool_size", "postgres", "concurrency", "rollback", "deploy"],
    red_herring_service="opensearch-cluster",
    metrics={
        "search-api": {
            "p50_latency_ms": 310, "p99_latency_ms": 5410, "error_rate_pct": 3.4,
            "cpu_pct": 29, "memory_pct": 48, "rps": 640,
            "note": "p99 stepped up at 09:10, shortly after the last deploy.",
        },
        "opensearch-cluster": {
            "p50_latency_ms": 42, "p99_latency_ms": 2100, "error_rate_pct": 0.4,
            "cpu_pct": 71, "memory_pct": 68,
            "note": "CPU elevated and p99 raised. Query volume unchanged from last week.",
        },
        "postgres-primary": {
            "p50_latency_ms": 6, "p99_latency_ms": 38, "error_rate_pct": 0.0,
            "cpu_pct": 64, "memory_pct": 59,
            "connections_active": 12, "connections_max": 300,
            "connection_wait_ms_p99": 4900,
            "note": (
                "Server-side query latency is healthy. CPU is elevated. Note the gap between "
                "active connections and the server maximum."
            ),
        },
        "user-profile-api": {
            "p50_latency_ms": 88, "p99_latency_ms": 240, "error_rate_pct": 0.1,
            "cpu_pct": 25, "memory_pct": 37, "rps": 300, "note": "Nominal.",
        },
        "web-frontend": {
            "p50_latency_ms": 420, "p99_latency_ms": 5900, "error_rate_pct": 2.9,
            "cpu_pct": 33, "memory_pct": 44, "rps": 1500,
            "note": "Degraded, but only on routes that call search-api.",
        },
    },
    logs={
        "search-api": [
            "2026-08-27T09:10:22Z WARN  db.pool  connection pool timeout after 4000ms (pool_size=12, waiters=38)",
            "2026-08-27T09:11:48Z WARN  db.pool  connection pool timeout after 4000ms (pool_size=12, waiters=44)",
            "2026-08-27T09:13:05Z ERROR search   facet lookup failed: pool exhausted",
            "2026-08-27T09:14:12Z ERROR http     GET /v1/search 503 in 5402ms",
        ],
        "opensearch-cluster": [
            "2026-08-27T09:12:00Z INFO  query  took=41ms hits=120",
            "2026-08-27T09:13:30Z INFO  query  took=48ms hits=98",
        ],
        "postgres-primary": [
            "2026-08-27T09:10:00Z INFO  conn  new connection storm from search-api (churn=340/min)",
            "2026-08-27T09:12:00Z INFO  conn  active=12 max=300",
        ],
    },
    deploys={
        "search-api": [
            {
                "deployed_at": "2026-08-27T09:09:00Z",
                "version": "v2026.08.27-1",
                "author": "team-discovery",
                "change_summary": "Raise worker_concurrency 15 -> 50 for autumn catalogue reindex",
            },
            {
                "deployed_at": "2026-08-20T15:30:00Z",
                "version": "v2026.08.20-2",
                "author": "team-discovery",
                "change_summary": "Add fuzzy matching to product name search",
            },
        ],
        "opensearch-cluster": [],
        "postgres-primary": [],
    },
    configs={
        "search-api": {
            "worker_concurrency": 50,
            "db.pool_size": 12,
            "db.pool_timeout_ms": 4000,
            "opensearch.timeout_ms": 3000,
            "_previous": {"worker_concurrency": 15, "db.pool_size": 12},
        },
        "opensearch-cluster": {"shards": 6, "replicas": 1},
        "postgres-primary": {"max_connections": 300},
    },
)


INCIDENTS: dict[str, Incident] = {
    INC_001.incident_id: INC_001,
    INC_002.incident_id: INC_002,
}


def get_incident(incident_id: str) -> Incident:
    try:
        return INCIDENTS[incident_id.strip().upper()]
    except KeyError:
        raise KeyError(
            f"Unknown incident {incident_id!r}. Known: {sorted(INCIDENTS)}"
        ) from None

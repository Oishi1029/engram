"""A synthetic production estate, and the two incidents the agent investigates.

All data here is fabricated. No real system, customer or person is represented — which is both
an ethical baseline and a contest requirement, since this data appears on screen in the demo video.

THE DESIGN THAT MAKES THE DEMO WORK
-----------------------------------
Both incidents are NOISY-NEIGHBOUR CONTENTION on a shared datastore:

    INC-001  checkout-api degrades  <-- user-profile-api's deploy drains the shared redis-session pool
    INC-002  search-api degrades    <-- notification-worker's deploy drains the shared postgres pool

Three properties make this the right shape, and an earlier draft that lacked them had to be thrown
away after the first live run:

1.  THE CULPRIT IS NOT THE SERVICE THAT REPORTED THE SYMPTOM, and it is not the obvious downstream
    dependency either. It is a SIBLING — another consumer of the same datastore. Finding it needs a
    real topology hop: notice the affected service has NOT deployed recently, ask who else uses its
    datastore, then check *their* deploys. An earlier version made each incident self-inflicted by
    the affected service, which reduced the task to "read the alert" — solved in ten steps with
    nothing worth remembering.

2.  THE TWO INCIDENTS SHARE NO SURFACE DETAIL. Different affected service, different datastore,
    different culprit, different red herring, different symptom. Only the causal shape repeats.
    Replaying one incident twice would demonstrate a cache; generalising to an unseen one is the
    claim this project actually makes.

3.  EACH CARRIES A RED HERRING that a memoryless agent reliably investigates first — a dependency
    whose metrics look alarming while its own error rate stays normal. The step-count delta between
    the cold and warm runs is measured against exactly that dead end.

`root_cause_service` is the service whose deploy CAUSED the incident. That is unambiguous, which
the earlier draft was not: it named the exhausted datastore while the misconfigured value actually
lived in the caller's config, so a correct answer graded as wrong.
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
    "cart-service": {
        "tier": "application",
        "language": "TypeScript",
        "owner": "team-storefront",
        "depends_on": ["redis-session", "inventory-api"],
    },
    "auth-gateway": {
        "tier": "edge",
        "language": "Go",
        "owner": "team-identity",
        "depends_on": ["redis-session"],
    },
    "recommendation-api": {
        "tier": "application",
        "language": "Python",
        "owner": "team-discovery",
        "depends_on": ["redis-session", "opensearch-cluster"],
    },
    "session-reaper": {
        "tier": "async",
        "language": "Go",
        "owner": "team-platform",
        "depends_on": ["redis-session"],
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
# INC-001 — the cold run.
#   Symptom in checkout-api. Culprit is user-profile-api, a SIBLING sharing redis-session.
#   Red herring: payments-api, downstream of checkout, slow only because its caller holds
#   connections open while waiting on sessions.
# --------------------------------------------------------------------------------------

INC_001 = Incident(
    incident_id="INC-001",
    title="Checkout p99 latency spiked to 8.2s; customers reporting timeouts at payment step",
    reported_at="2026-08-26T14:02:00Z",
    affected_service="checkout-api",
    root_cause_service="user-profile-api",
    root_cause_summary=(
        "user-profile-api deployed at 13:58 raising worker_concurrency 20 -> 120 and its "
        "redis.pool_size 25 -> 150 for a profile backfill. redis-session allows 200 connections "
        "in total, so user-profile-api now holds the overwhelming majority of them and "
        "checkout-api — which has not deployed in three days and is unchanged — is starved. "
        "payments-api looks slow only because checkout-api holds its calls open."
    ),
    correct_remediation_keywords=["user-profile", "concurrency", "pool_size", "rollback", "backfill"],
    red_herring_service="payments-api",
    metrics={
        "checkout-api": {
            "p50_latency_ms": 240, "p99_latency_ms": 8210, "error_rate_pct": 4.1,
            "cpu_pct": 34, "memory_pct": 51, "rps": 820,
            "note": "p99 stepped up sharply at 13:59. Traffic volume is unchanged week on week.",
        },
        "payments-api": {
            "p50_latency_ms": 180, "p99_latency_ms": 5900, "error_rate_pct": 0.2,
            "cpu_pct": 22, "memory_pct": 44, "rps": 780,
            "note": "p99 elevated, but error rate is normal and CPU is low.",
        },
        "redis-session": {
            "p50_latency_ms": 1, "p99_latency_ms": 3, "error_rate_pct": 0.0,
            "cpu_pct": 41, "memory_pct": 55,
            "connections_active": 200, "connections_max": 200,
            "connection_wait_ms_p99": 7400,
            "note": (
                "Server-side latency is healthy. Connections are pinned at the maximum. "
                "Per-client attribution is not exported by this cluster."
            ),
        },
        "user-profile-api": {
            "p50_latency_ms": 88, "p99_latency_ms": 240, "error_rate_pct": 0.1,
            "cpu_pct": 78, "memory_pct": 61, "rps": 300,
            "note": "Healthy latency. CPU markedly higher than its weekly baseline of 25%.",
        },
        "inventory-api": {
            "p50_latency_ms": 95, "p99_latency_ms": 310, "error_rate_pct": 0.1,
            "cpu_pct": 28, "memory_pct": 39, "rps": 410, "note": "Nominal.",
        },
        "cart-service": {
            "p50_latency_ms": 130, "p99_latency_ms": 2900, "error_rate_pct": 1.2,
            "cpu_pct": 26, "memory_pct": 41, "rps": 350,
            "note": "Mildly degraded. Also a consumer of redis-session.",
        },
        "auth-gateway": {
            "p50_latency_ms": 40, "p99_latency_ms": 1800, "error_rate_pct": 0.6,
            "cpu_pct": 19, "memory_pct": 33, "rps": 2100,
            "note": "Mildly degraded. Also a consumer of redis-session.",
        },
        "recommendation-api": {
            "p50_latency_ms": 210, "p99_latency_ms": 640, "error_rate_pct": 0.2,
            "cpu_pct": 24, "memory_pct": 36, "rps": 180, "note": "Largely nominal.",
        },
        "session-reaper": {
            "p50_latency_ms": 15, "p99_latency_ms": 60, "error_rate_pct": 0.0,
            "cpu_pct": 12, "memory_pct": 20, "note": "Nominal.",
        },
        "postgres-primary": {
            "p50_latency_ms": 4, "p99_latency_ms": 22, "error_rate_pct": 0.0,
            "cpu_pct": 31, "memory_pct": 55,
            "connections_active": 84, "connections_max": 300, "note": "Nominal.",
        },
    },
    logs={
        "checkout-api": [
            "2026-08-26T13:59:14Z WARN  redis.pool  connection pool timeout after 5000ms (pool_size=40, in_use=40, waiters=47)",
            "2026-08-26T14:00:02Z WARN  redis.pool  connection pool timeout after 5000ms (pool_size=40, in_use=40, waiters=51)",
            "2026-08-26T14:01:37Z ERROR checkout    session lookup failed: could not acquire redis connection",
            "2026-08-26T14:02:11Z WARN  upstream    payments-api call exceeded 5s budget (held open awaiting session)",
        ],
        "payments-api": [
            "2026-08-26T14:01:40Z INFO  http  POST /v1/charge 200 in 174ms",
            "2026-08-26T14:02:03Z INFO  http  POST /v1/charge 200 in 191ms",
        ],
        "redis-session": [
            "2026-08-26T13:58:40Z INFO  clients  connected_clients rising: 62 -> 200 over 90s",
            "2026-08-26T14:02:00Z WARN  clients  max number of clients reached (200)",
        ],
        "user-profile-api": [
            "2026-08-26T13:58:30Z INFO  startup  worker_concurrency=120 redis.pool_size=150",
            "2026-08-26T13:59:00Z INFO  backfill profile backfill started, 2.1M records queued",
        ],
    },
    deploys={
        "checkout-api": [
            {
                "deployed_at": "2026-08-23T09:12:00Z",
                "version": "v2026.08.23-1",
                "author": "team-payments",
                "change_summary": "Add idempotency key to charge endpoint",
            }
        ],
        "payments-api": [
            {
                "deployed_at": "2026-08-21T11:40:00Z",
                "version": "v2026.08.21-2",
                "author": "team-payments",
                "change_summary": "Upgrade Go runtime to 1.25",
            }
        ],
        "user-profile-api": [
            {
                "deployed_at": "2026-08-26T13:58:00Z",
                "version": "v2026.08.26-4",
                "author": "team-identity",
                "change_summary": "Raise worker_concurrency 20 -> 120 and redis.pool_size 25 -> 150 for profile backfill",
            }
        ],
        "cart-service": [
            {
                "deployed_at": "2026-08-25T10:05:00Z",
                "version": "v2026.08.25-1",
                "author": "team-storefront",
                "change_summary": "Fix currency rounding on cart totals",
            }
        ],
        "auth-gateway": [
            {
                "deployed_at": "2026-08-19T08:20:00Z",
                "version": "v2026.08.19-1",
                "author": "team-identity",
                "change_summary": "Rotate JWT signing key",
            }
        ],
        "recommendation-api": [],
        "session-reaper": [
            {
                "deployed_at": "2026-08-22T16:45:00Z",
                "version": "v2026.08.22-3",
                "author": "team-platform",
                "change_summary": "Lower reaper sweep interval 300s -> 240s",
            }
        ],
        "redis-session": [],
    },
    configs={
        "checkout-api": {
            "worker_concurrency": 20,
            "redis.pool_size": 40,
            "redis.pool_timeout_ms": 5000,
            "payments.timeout_ms": 5000,
            "_previous": {"worker_concurrency": 20, "redis.pool_size": 40},
        },
        "payments-api": {"worker_concurrency": 40, "db.pool_size": 40},
        "cart-service": {
            "worker_concurrency": 25, "redis.pool_size": 30,
            "_previous": {"worker_concurrency": 25, "redis.pool_size": 30},
        },
        "auth-gateway": {
            "worker_concurrency": 60, "redis.pool_size": 50,
            "_previous": {"worker_concurrency": 60, "redis.pool_size": 50},
        },
        "session-reaper": {
            "sweep_interval_s": 240, "redis.pool_size": 5,
            "_previous": {"sweep_interval_s": 300, "redis.pool_size": 5},
        },
        "user-profile-api": {
            "worker_concurrency": 120,
            "redis.pool_size": 150,
            "_previous": {"worker_concurrency": 20, "redis.pool_size": 25},
        },
        "redis-session": {"maxclients": 200},
    },
)


# --------------------------------------------------------------------------------------
# INC-002 — the warm run.
#   Same causal shape, zero shared surface. Symptom in search-api; culprit is
#   notification-worker, an ASYNC service most engineers would never think to look at,
#   sharing postgres-primary. Red herring: opensearch-cluster, genuinely CPU-hot.
# --------------------------------------------------------------------------------------

INC_002 = Incident(
    incident_id="INC-002",
    title="Search p99 latency spiked to 5.4s with elevated 5xx; catalogue browse degraded",
    reported_at="2026-08-27T09:14:00Z",
    affected_service="search-api",
    root_cause_service="notification-worker",
    root_cause_summary=(
        "notification-worker deployed at 09:09 raising batch_concurrency 10 -> 200 and its "
        "db.pool_size 15 -> 220 for a campaign send. postgres-primary allows 300 connections, so "
        "the worker now holds most of them and search-api — unchanged, last deployed seven days "
        "ago — cannot acquire one. opensearch CPU is elevated for an unrelated reindex and is a "
        "dead end."
    ),
    correct_remediation_keywords=["notification", "batch_concurrency", "pool_size", "rollback"],
    red_herring_service="opensearch-cluster",
    metrics={
        "search-api": {
            "p50_latency_ms": 310, "p99_latency_ms": 5410, "error_rate_pct": 3.4,
            "cpu_pct": 29, "memory_pct": 48, "rps": 640,
            "note": "p99 stepped up at 09:10. Query volume unchanged week on week.",
        },
        "opensearch-cluster": {
            "p50_latency_ms": 42, "p99_latency_ms": 2100, "error_rate_pct": 0.4,
            "cpu_pct": 71, "memory_pct": 68,
            "note": "CPU elevated and p99 raised. A scheduled reindex has been running since 06:00.",
        },
        "postgres-primary": {
            "p50_latency_ms": 6, "p99_latency_ms": 38, "error_rate_pct": 0.0,
            "cpu_pct": 64, "memory_pct": 59,
            "connections_active": 300, "connections_max": 300,
            "connection_wait_ms_p99": 4900,
            "note": (
                "Server-side query latency is healthy. Connections are pinned at the maximum. "
                "Per-client attribution is not exported by this instance."
            ),
        },
        "notification-worker": {
            "p50_latency_ms": 120, "p99_latency_ms": 480, "error_rate_pct": 0.0,
            "cpu_pct": 83, "memory_pct": 70,
            "note": "Healthy latency. CPU far above its weekly baseline of 20%.",
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
            "2026-08-27T09:10:22Z WARN  db.pool  connection pool timeout after 4000ms (pool_size=12, in_use=11, waiters=38)",
            "2026-08-27T09:11:48Z WARN  db.pool  could not acquire postgres connection; server refused new client",
            "2026-08-27T09:13:05Z ERROR search   facet lookup failed: no connection available",
            "2026-08-27T09:14:12Z ERROR http     GET /v1/search 503 in 5402ms",
        ],
        "opensearch-cluster": [
            "2026-08-27T06:00:00Z INFO  reindex  scheduled reindex started",
            "2026-08-27T09:12:00Z INFO  query    took=41ms hits=120",
        ],
        "postgres-primary": [
            "2026-08-27T09:09:30Z INFO  conn  active connections rising: 96 -> 300 over 120s",
            "2026-08-27T09:12:00Z WARN  conn  connection limit reached: 300/300; refusing new clients",
        ],
        "notification-worker": [
            "2026-08-27T09:09:20Z INFO  startup  batch_concurrency=200 db.pool_size=220",
            "2026-08-27T09:10:00Z INFO  campaign autumn campaign send started, 4.8M recipients queued",
        ],
    },
    deploys={
        "search-api": [
            {
                "deployed_at": "2026-08-20T15:30:00Z",
                "version": "v2026.08.20-2",
                "author": "team-discovery",
                "change_summary": "Add fuzzy matching to product name search",
            }
        ],
        "opensearch-cluster": [],
        "postgres-primary": [],
        "notification-worker": [
            {
                "deployed_at": "2026-08-27T09:09:00Z",
                "version": "v2026.08.27-1",
                "author": "team-comms",
                "change_summary": "Raise batch_concurrency 10 -> 200 and db.pool_size 15 -> 220 for autumn campaign send",
            }
        ],
    },
    configs={
        "search-api": {
            "worker_concurrency": 15,
            "db.pool_size": 12,
            "db.pool_timeout_ms": 4000,
            "opensearch.timeout_ms": 3000,
            "_previous": {"worker_concurrency": 15, "db.pool_size": 12},
        },
        "opensearch-cluster": {"shards": 6, "replicas": 1},
        "postgres-primary": {"max_connections": 300},
        "notification-worker": {
            "batch_concurrency": 200,
            "db.pool_size": 220,
            "_previous": {"batch_concurrency": 10, "db.pool_size": 15},
        },
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

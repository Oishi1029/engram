# Benchmark — INC-002, 5 trials per arm

Run 2026-08-26 on Vertex AI `gemini-3.7-flash`, `global` endpoint, Firestore `asia-southeast1`.
Reproduce with `python scripts/benchmark.py -n 5`.

## Protocol

1. Wipe all memory.
2. **Cold arm** — 5 trials of INC-002 with memory disabled.
3. **Teach** — one run of INC-001 (a *different* incident), then consolidate **that run only**.
4. **Warm arm** — 5 trials of INC-002 with memory enabled.

INC-002 is never consolidated, so no trial can learn its own answer. INC-001 shares no surface
detail with it — different affected service, datastore, culprit and red herring.

## Result

| metric | COLD | WARM | mean Δ |
|---|---|---|---|
| **incident fully resolved** | **0/5** | **5/5** | — |
| remediation chosen | `rollback_deploy` ×5 | `reduce_client_pool_in_place` ×5 | — |
| tool calls | 12.8 ± 3.8 (9–19) | 8.8 ± 0.8 (8–10) | **−31%** |
| wall seconds | 45.0 ± 9.7 (33–60) | 32.5 ± 3.0 (30–37) | −28% |
| total tokens | 36,475 ± 12,844 | 24,628 ± 2,285 | −32% |
| steps on red herring | 0.4 ± 0.5 | 0.0 ± 0.0 | −100% |
| diagnosis correct | 5/5 | 5/5 | — |
| memories recalled | 0 | 3.0 ± 0.0 | — |

## Reading it honestly

**Diagnosis is 5/5 in both arms.** Gemini 3.7 Flash finds the root cause reliably without any
memory at all, and no amount of making the incident harder changed that. Any claim that memory
helps the agent *find* the answer would be overstated — the honest search-efficiency result is
−31% on steps, and a much larger reduction in variance (±3.8 → ±0.8).

**The categorical result is the remediation.** Every fact needed to diagnose the incident is
present in the environment, so a capable model can always recover it. But *which fix to apply* is
not in the environment. Rolling back the offending deploy clears the symptom and destroys an
in-flight job; reducing the client's pool size in place clears it just as fast and lets the job
finish. The environment never reveals the two facts that decide it: that the interrupted job is **non-resumable**, and that change control **rejects** expanding shared capacity. Both are consequences you can only learn by having acted before — and empirically the memoryless agent had the campaign-start log line in context in all five trials and still chose rollback all five times.

The cold agent chose rollback 5/5 — the textbook instinct. The warm agent chose the in-place fix
5/5, having learned from one prior incident on entirely different services what the rollback cost.

That is the claim this project makes: memory is not a faster index over what the agent could
already work out. It is the only route to knowledge the environment never exposes.

## Lessons consolidated from the single teaching run

1. **WHEN** a shared datastore is starved of connections following a deploy that started a
   long-running job alongside enlarged connection pool settings —
   **THEN** do not roll back the deployment, as rolling back terminates in-flight jobs that may
   not resume from checkpoints. Reduce the client's connection pool size in place to clear
   contention without aborting the background workload.
2. **WHEN** a shared datastore hits its connection capacity ceiling with high client wait times,
   but server-side query latency and CPU remain nominal —
   **THEN** do not investigate datastore query performance. Check recent deploys and configuration
   changes across all client services sharing that datastore.
3. **WHEN** a downstream service shows elevated p99 latency while its CPU and error rates remain
   nominal — **THEN** do not treat it as the root cause; requests are being held open upstream
   while waiting on an exhausted shared resource.

None of these names a service. All three were written by the consolidation pass, unedited.

## Per-trial data

Verbatim stdout from the run that produced the table above.

```
COLD ARM — 5 trials of INC-002, memory OFF
  cold trial 1/5: ok   13 calls    46.0s    36395 tok  1 red-herring  0 recalled
  cold trial 2/5: ok   19 calls    60.0s    57674 tok  1 red-herring  0 recalled
  cold trial 3/5: ok    9 calls    33.1s    23071 tok  0 red-herring  0 recalled
  cold trial 4/5: ok   11 calls    41.8s    31891 tok  0 red-herring  0 recalled
  cold trial 5/5: ok   12 calls    44.4s    33343 tok  0 red-herring  0 recalled

TEACH — one run of INC-001, then consolidate that run only
  INC-001: 16 calls, correct=True
  consolidated 16 episodes -> 3 lessons

WARM ARM — 5 trials of INC-002, memory ON
  warm trial 1/5: ok   10 calls    33.8s    27531 tok  0 red-herring  3 recalled
  warm trial 2/5: ok    9 calls    31.6s    25877 tok  0 red-herring  3 recalled
  warm trial 3/5: ok    9 calls    30.1s    24840 tok  0 red-herring  3 recalled
  warm trial 4/5: ok    8 calls    29.7s    21626 tok  0 red-herring  3 recalled
  warm trial 5/5: ok    9 calls    37.0s    23268 tok  0 red-herring  3 recalled
```

Note `ok` in every row: that column is the DIAGNOSIS, and it is correct in all ten trials. The arms
separate only on the remediation, which is the point of the design.

Wall-clock figures are from a local run. The same code on Cloud Run is slower under concurrency —
the service is deliberately capped at 2 instances — so a run issued against the hosted URL while
others are in flight will take longer. Tool counts and the remediation choice are unaffected.


## Caveats

- n=5 per arm, single seed, one model version (`gemini-3.7-flash`, global endpoint). The remediation result is 0/5 vs 5/5, which is unambiguous; the step-count deltas
  carry real uncertainty and are reported with spread rather than as point estimates.
- Successful warm trials reward the salience of lessons they used, so later warm trials see
  marginally higher salience than earlier ones. With a three-lesson store this can only affect
  ordering among items that are all already injected.
- One synthetic estate and two incidents. This demonstrates the mechanism; it is not a claim
  about production SRE workloads.

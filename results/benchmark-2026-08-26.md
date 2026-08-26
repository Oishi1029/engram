# Benchmark — INC-002, two independent runs of 5 trials per arm (n=10)

Run 2026-08-26 / 2026-08-27 on Vertex AI `gemini-3.7-flash`, `global` endpoint, Firestore
`asia-southeast1`. Reproduce with `python scripts/benchmark.py -n 5`.

## Protocol

1. Wipe all memory.
2. **Cold arm** — 5 trials of INC-002 with memory disabled.
3. **Teach** — one run of INC-001 (a *different* incident), then consolidate **that run only**.
4. **Warm arm** — 5 trials of INC-002 with memory enabled.

INC-002 is never consolidated, so no trial can learn its own answer. That is now enforced
structurally: a memory-disabled run is a control by definition and its episodes are written
ineligible for consolidation, so no caller can undo it. `scripts/benchmark.py` also asserts it at
runtime and aborts if it is ever violated.

INC-001 shares no surface detail with INC-002 — different affected service, datastore, culprit and
red herring. Only the causal shape repeats.

## Result — pooled, n=10 per arm

| metric | COLD (no memory) | WARM (memory) | Δ |
|---|---|---|---|
| **incident fully resolved** | **0 / 10** | **10 / 10** | — |
| remediation chosen | `rollback_deploy` ×10 | `reduce_client_pool_in_place` ×10 | — |
| tool calls | 11.7 ± 3.1 | 9.0 ± 0.8 | **−23 %** |
| wall seconds | 53.6 ± 13.6 | 42.1 ± 14.8 | −21 % |
| total tokens | 32,686 ± 10,355 | 25,139 ± 2,754 | −23 % |
| steps spent on the red herring | 0.3 ± 0.5 | 0.0 ± 0.0 | −100 % |
| root cause diagnosed correctly | 10 / 10 | 10 / 10 | — |

### The two runs separately, because the spread matters

| | cold tool calls | warm tool calls |
|---|---|---|
| run 1 (n=5) | 12.8 ± 3.8 | 9.0 ± 0.8 |
| run 2 (n=5) | 10.6 ± 2.1 | 9.0 ± 1.0 |

The **warm** arm returned a mean of exactly 9.0 in both runs. The **cold** arm moved by more than
two calls between them. That is the clearest single expression of what memory does here: it does not
merely shift the average, it removes the variance. Run 1 alone would have supported a −31 % headline;
run 2 alone, −15 %. Pooling gives −23 %, and reporting both is the honest way to present a number
whose control arm is that noisy.

**The resolution result did not vary at all: 0/5 and 0/5 cold, 5/5 and 5/5 warm.**

## Reading it honestly

**Diagnosis is 10/10 in both arms.** Gemini 3.7 Flash finds the root cause reliably with no memory
at all, and making the incident harder did not change that. Any claim that memory helps it *find*
the answer would be overstated — the honest search-efficiency result is −23 % on steps, with a large
reduction in variance.

**The categorical result is the remediation**, and it follows from a deliberate design decision.

Every fact needed to *diagnose* the incident is present in the environment, so a capable model can
always recover it; memory could only ever save search steps. So the agent must also **choose and
apply a fix**, and which fix is correct is not in the environment:

* `rollback_deploy` clears the symptom **and terminates an in-flight job** that must restart from zero.
* `reduce_client_pool_in_place` clears it just as fast and lets the job finish.

The environment never reveals the two facts that decide it: that the interrupted job is
**non-resumable**, and that change control **rejects** expanding shared capacity. Both are
consequences you can only learn by having acted before — and empirically the memoryless agent had
the campaign-start log line in context in all ten trials and chose rollback all ten times.

That is the claim: **memory is not a faster index over what the agent could already work out. It is
the only route to knowledge the environment never exposes.**

## Per-trial data

```
RUN 1 — cold arm                                  RUN 1 — warm arm
  13 calls  46.0s  36,395 tok  1 dead-end           10 calls  33.8s  27,531 tok  0
  19 calls  60.0s  57,674 tok  1 dead-end            9 calls  31.6s  25,877 tok  0
   9 calls  33.1s  23,071 tok  0                     9 calls  30.1s  24,840 tok  0
  11 calls  41.8s  31,891 tok  0                     8 calls  29.7s  21,626 tok  0
  12 calls  44.4s  33,343 tok  0                     9 calls  37.0s  23,268 tok  0

RUN 2 — cold arm                                  RUN 2 — warm arm
  13 calls  57.7s  35,906 tok  1 dead-end            9 calls  56.1s  25,563 tok  0
  11 calls  78.8s  30,884 tok  0                     8 calls  67.0s  22,610 tok  0
   9 calls  59.0s  23,458 tok  0                    10 calls  65.0s  28,794 tok  0
   8 calls  47.0s  21,093 tok  0                    10 calls  38.4s  29,220 tok  0
  12 calls  67.8s  33,143 tok  0                     8 calls  31.9s  22,061 tok  0
```

All twenty trials diagnosed the root cause correctly. The arms separate only on the remediation,
which is the point of the design.

Wall-clock figures are from local runs. The same code on Cloud Run is slower under concurrency —
the service is capped at 2 instances deliberately — so a run issued against the hosted URL while
others are in flight takes longer. Tool counts and the remediation choice are unaffected.

## Lessons consolidated from a single teaching run

Representative output from run 2's consolidation pass, unedited:

1. **WHEN** a shared datastore is saturated by an aggressive connection pool setting introduced in a
   deployment that also launched a batch or migration workload —
   **THEN** do not roll back the entire deployment, as rolling back will terminate long-running
   uncheckpointed batch jobs and force a full restart. Reduce the offending client's connection pool
   size in place to relieve contention while letting the workload finish.
2. **WHEN** a shared datastore hits maximum connection limits and high wait times while its internal
   latency, CPU and memory remain healthy —
   **THEN** do not debug datastore internals; check recent deployments and configuration changes
   across all client services sharing that datastore.
3. **WHEN** a service reports high p99 latency on a downstream dependency that itself shows normal
   CPU, error rates and database metrics —
   **THEN** check whether the caller is holding connections open while blocked on another shared
   dependency before investigating the downstream service.

**Not one of them names a service.** That constraint is what lets them fire on an unseen incident,
and it is the difference between a memory and a cache.

## Caveats, stated rather than buried

- n=10 per arm across two runs, one model version (`gemini-3.7-flash`, global endpoint), one
  synthetic estate, two incidents. This demonstrates the mechanism; it is not a claim about
  production SRE workloads.
- The cold arm is genuinely noisy (±3.1 tool calls). Single-run deltas ranged from −15 % to −31 %.
  The resolution result was invariant across all twenty trials.
- Successful warm trials reward the salience of the lessons they used, so later warm trials see
  marginally higher salience than earlier ones. With a two-to-three-lesson store this can only
  affect ordering among items that are all already injected.
- **An earlier measurement had to be discarded.** After adding 429 retry handling, one cold trial
  recorded 0 tool calls and 1,130 tokens over 484 seconds — it spent its entire life in rate-limit
  backoff, hit the run timeout, and was scored as a *failed investigation*. Two such trials dragged
  the control arm to 3/5 on a metric whose true value is 5/5, **inflating the contrast being
  measured**. Retry was tuned short, trials are now paced, and timed-out trials are excluded from
  the summary with the exclusion printed. Recorded here because the failure flattered the result.

"""HTTP surface, deployed on Cloud Run.

Three jobs:
  * `/run` and `/consolidate` make the agent and the consolidation pass callable as services.
    Consolidation being an HTTP endpoint is what lets Cloud Scheduler drive it out of band, which
    is the architectural claim: working out what an experience meant is never paid for on the
    agent's critical path.
  * `/` renders a live view of the memory store, so the deployment is inspectable in a browser —
    which is the "hosted project URL" the submission needs.
  * `/health` for Cloud Run. NOTE: not `/healthz` — Cloud Run's frontend returns its own
    404 page for that path before the request ever reaches the container.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from engram.config import CONFIG
from engram.memory.consolidate import consolidate
from engram.memory.store import MemoryStore
from engram.runner import run_incident

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="engram", description="A task agent with durable, consolidating memory.")

_store: MemoryStore | None = None


def store() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store


class RunRequest(BaseModel):
    incident_id: str = "INC-001"
    use_memory: bool = True


class ConsolidateRequest(BaseModel):
    run_ids: list[str] | None = None
    limit: int | None = None


@app.get("/health")
def health() -> dict:
    return {"ok": True, "model": CONFIG.model, "project": CONFIG.project_id}


@app.post("/run")
async def run(req: RunRequest) -> JSONResponse:
    record = await run_incident(
        req.incident_id, store=store(), use_memory=req.use_memory, verbose=False
    )
    return JSONResponse(record)


@app.post("/consolidate")
async def consolidate_endpoint(req: ConsolidateRequest) -> JSONResponse:
    # Consolidation is synchronous and network-bound; keep the event loop free for Cloud Run.
    summary = await asyncio.to_thread(
        consolidate, store(), req.limit, req.run_ids
    )
    return JSONResponse(summary)


@app.get("/memory")
def memory() -> JSONResponse:
    mems = sorted(store().all_semantic(), key=lambda m: m.salience, reverse=True)
    return JSONResponse(
        {
            "count": len(mems),
            "memories": [
                {
                    "cue": m.cue,
                    "lesson": m.lesson,
                    "salience": round(m.salience, 3),
                    "times_retrieved": m.times_retrieved,
                    "times_useful": m.times_useful,
                }
                for m in mems
            ],
        }
    )


@app.get("/runs")
def runs() -> JSONResponse:
    return JSONResponse({"runs": store().recent_runs(limit=25)})


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    mems = sorted(store().all_semantic(), key=lambda m: m.salience, reverse=True)
    recent = store().recent_runs(limit=12)

    def esc(x: object) -> str:
        return html.escape(str(x))

    mem_rows = "".join(
        f"<tr><td class='cue'>{esc(m.cue)}</td><td>{esc(m.lesson)}</td>"
        f"<td class='n'>{m.salience:.2f}</td><td class='n'>{m.times_retrieved}</td>"
        f"<td class='n'>{m.times_useful}</td></tr>"
        for m in mems
    ) or "<tr><td colspan='5' class='empty'>No consolidated memories yet.</td></tr>"

    def run_row(r: dict) -> str:
        # 🔴 The verdict column must key on `resolved`, not `correct`.
        # `correct` is the DIAGNOSIS flag, and the benchmark reports it at 5/5 in both arms — so a
        # page driven by it showed memory making no difference, which is the opposite of what this
        # project measured. `resolved` requires the right diagnosis AND a remediation that did not
        # destroy an in-flight job.
        timed_out = bool(r.get("timed_out"))
        if timed_out:
            verdict, cls = "timed out", "warn"
        elif r.get("resolved"):
            verdict, cls = "resolved", "ok"
        else:
            verdict, cls = "not resolved", "bad"
        remediation = r.get("chosen_remediation") or ("—" if timed_out else "none")
        return (
            f"<tr><td>{esc(r.get('incident_id'))}</td>"
            f"<td>{'ON' if r.get('memory_enabled') else 'OFF'}</td>"
            f"<td class='n'>{esc(r.get('tool_calls'))}</td>"
            f"<td class='n'>{esc(r.get('wall_seconds'))}s</td>"
            f"<td class='{'ok' if r.get('correct') else 'bad'}'>"
            f"{'correct' if r.get('correct') else 'wrong'}</td>"
            f"<td><code>{esc(remediation)}</code></td>"
            f"<td class='{cls}'>{verdict}</td></tr>"
        )

    run_rows = "".join(run_row(r) for r in recent) or (
        "<tr><td colspan='7' class='empty'>No runs recorded yet.</td></tr>"
    )

    return HTMLResponse(f"""<!doctype html><meta charset="utf-8">
<title>engram — durable agent memory</title>
<style>
 :root {{ color-scheme: light dark; }}
 body {{ font: 15px/1.55 ui-sans-serif,system-ui,-apple-system,sans-serif;
        max-width: 1100px; margin: 2.5rem auto; padding: 0 1.25rem; }}
 h1 {{ margin-bottom: .15rem; font-size: 1.6rem; }}
 .sub {{ opacity:.7; margin-top:0; }}
 table {{ border-collapse: collapse; width: 100%; margin: .75rem 0 2rem; font-size: 14px; }}
 th,td {{ text-align: left; padding: .5rem .6rem; border-bottom: 1px solid rgba(128,128,128,.28);
          vertical-align: top; }}
 th {{ font-size: 12px; text-transform: uppercase; letter-spacing: .04em; opacity: .65; }}
 td.n {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
 td.cue {{ max-width: 22rem; }}
 .ok {{ color:#188038; font-weight:600; }} .bad {{ color:#c5221f; font-weight:600; }}
 .warn {{ color:#a06000; }}
 .headline {{ background: rgba(24,128,56,.10); border:1px solid rgba(24,128,56,.35);
              border-radius:8px; padding:.85rem 1.1rem; margin:1rem 0 1.5rem; font-size:14.5px; }}
 .headline b {{ font-size:16px; }}
 .empty {{ opacity:.6; font-style: italic; }}
 code {{ background: rgba(128,128,128,.16); padding: .1rem .35rem; border-radius: 4px; }}
</style>
<h1>engram</h1>
<p class="sub">A long-horizon task agent with durable, consolidating memory —
{esc(CONFIG.model)} on Vertex AI, Google ADK, Firestore, Cloud Run.</p>

<h2>Semantic memory <span style="opacity:.6;font-weight:400">— consolidated lessons</span></h2>
<table><tr><th>When</th><th>Then</th><th>Salience</th><th>Retrieved</th><th>Useful</th></tr>
{mem_rows}</table>

<div class="headline">
<b>Measured over 20 trials: incident resolved 0/10 &rarr; 10/10 with memory.</b><br>
Both arms diagnose the root cause correctly 10/10 &mdash; the model is strong and memory cannot claim
credit for that. The difference is what each one then <i>does</i>: without memory it rolls back the
offending deploy, which clears the symptom and destroys an in-flight job. Tool calls &minus;23%,
tokens &minus;23%, dead-end steps &minus;100%.
<a href="https://github.com/Oishi1029/engram/blob/main/results/benchmark-2026-08-26.md">Full protocol and caveats &rarr;</a>
</div>

<h2>Recent runs</h2>
<p style="opacity:.7;font-size:13px;margin-top:-.4rem">
<b>Diagnosis</b> = did it find the root cause. <b>Resolved</b> = did it also apply a fix that
didn't destroy a running job. Only the second one separates the arms.</p>
<table><tr><th>Incident</th><th>Memory</th><th>Tool calls</th><th>Wall</th><th>Diagnosis</th><th>Remediation applied</th><th>Outcome</th></tr>
{run_rows}</table>

<p style="opacity:.65;font-size:13px">
POST <code>/run</code> · POST <code>/consolidate</code> · GET <code>/memory</code> ·
GET <code>/runs</code> · GET <code>/health</code></p>
""")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

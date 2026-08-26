"""HTTP surface, deployed on Cloud Run.

Three jobs:
  * `/run` and `/consolidate` make the agent and the consolidation pass callable as services.
    Consolidation being an HTTP endpoint is what lets Cloud Scheduler drive it out of band, which
    is the architectural claim: working out what an experience meant is never paid for on the
    agent's critical path.
  * `/` renders a live view of the memory store, so the deployment is inspectable in a browser —
    which is the "hosted project URL" the submission needs.
  * `/healthz` for Cloud Run.
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


@app.get("/healthz")
def healthz() -> dict:
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

    run_rows = "".join(
        f"<tr><td>{esc(r.get('incident_id'))}</td>"
        f"<td>{'ON' if r.get('memory_enabled') else 'OFF'}</td>"
        f"<td class='n'>{esc(r.get('tool_calls'))}</td>"
        f"<td class='n'>{esc(r.get('wall_seconds'))}s</td>"
        f"<td class='n'>{esc(r.get('total_tokens'))}</td>"
        f"<td class='{'ok' if r.get('correct') else 'bad'}'>"
        f"{'correct' if r.get('correct') else 'wrong'}</td></tr>"
        for r in recent
    ) or "<tr><td colspan='6' class='empty'>No runs recorded yet.</td></tr>"

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
 .ok {{ color:#188038; }} .bad {{ color:#c5221f; }}
 .empty {{ opacity:.6; font-style: italic; }}
 code {{ background: rgba(128,128,128,.16); padding: .1rem .35rem; border-radius: 4px; }}
</style>
<h1>engram</h1>
<p class="sub">A long-horizon task agent with durable, consolidating memory —
{esc(CONFIG.model)} on Vertex AI, Google ADK, Firestore, Cloud Run.</p>

<h2>Semantic memory <span style="opacity:.6;font-weight:400">— consolidated lessons</span></h2>
<table><tr><th>When</th><th>Then</th><th>Salience</th><th>Retrieved</th><th>Useful</th></tr>
{mem_rows}</table>

<h2>Recent runs</h2>
<table><tr><th>Incident</th><th>Memory</th><th>Tool calls</th><th>Wall</th><th>Tokens</th><th>Result</th></tr>
{run_rows}</table>

<p style="opacity:.65;font-size:13px">
POST <code>/run</code> · POST <code>/consolidate</code> · GET <code>/memory</code> ·
GET <code>/runs</code> · GET <code>/healthz</code></p>
""")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

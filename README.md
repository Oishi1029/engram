# engram

**A long-horizon task agent with durable, consolidating memory.**

An autonomous incident-triage agent that runs a real multi-step investigation, writes what it
learned to durable memory, consolidates that memory in the background, and then handles a
*different but related* incident measurably better because of it.

> Built for the [All Things Agentic Hackathon](https://allthingsagentichackathon.devpost.com/)
> (Google Cloud, August 2026). Category: **The Taskmaster**.

## Status

🚧 Under active development during the Submission Period (3–31 August 2026).

## Stack

| Layer | Technology |
|---|---|
| Model | **Gemini 3.7 Flash** via **Vertex AI** |
| Agent framework | **Google ADK** (Agent Development Kit) |
| Compute | **Cloud Run** |
| Memory store | **Firestore** (native mode) |
| Background consolidation | **Cloud Scheduler** → Cloud Run |

## Pre-existing work disclosure

This project was newly created during the Submission Period (3–31 August 2026); every commit in
this repository falls inside that window. Its memory architecture was informed by my own prior
open-source project [`instar`](https://github.com/Oishi1029/instar), which is disclosed here as
pre-existing work of mine. **No source code from that project was copied into this one.** AI coding
assistants were used throughout development, as expressly permitted by the Contest Rules.

## Licence

MIT — see [LICENSE](LICENSE).

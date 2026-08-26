"""Run a single incident. Usage: python scripts/run_one.py INC-001 [--no-memory]"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engram.runner import run_incident_sync

incident = sys.argv[1] if len(sys.argv) > 1 else "INC-001"
use_memory = "--no-memory" not in sys.argv
rec = run_incident_sync(incident, use_memory=use_memory)
print("\nRUN RECORD:")
print(json.dumps({k: v for k, v in rec.items() if k != "started_at"}, indent=2))

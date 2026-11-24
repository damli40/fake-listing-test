#!/usr/bin/env python3
"""Runs phase 2 block by block, exactly as TEST-PLAN-API-2.md says: each block starts only if
its projected cost fits under the cap, and its answers are judged before the next starts."""
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import run2    # noqa: E402

DATA, SCHED, CAP = "data-2", "schedule-api-2.json", 28.0
schedule = json.loads((ROOT / SCHED).read_text())


def sh(*args):
    print("\n$ " + " ".join(args), flush=True)
    return subprocess.run([sys.executable, "-u"] + list(args), cwd=ROOT).returncode


log = []
for block in ["A", "B1", "B2", "B3", "B4"]:
    extra = []
    if block == "B4":
        plan = run2.wonder_plan(run2.spent_in(ROOT / DATA), run2.pilot_mean(ROOT / DATA, schedule, "muse-wonder-1.0"), CAP)
        print("wonder plan: %s (counted so far $%.2f)" % (plan, run2.spent_in(ROOT / DATA)), flush=True)
        log.append("B4 plan: %s" % plan)
        if plan == "skip":
            continue
        if plan == "three":
            extra = ["--only-conditions", "control", "returns", "inject"]
    rc = sh("run2.py", "--data", DATA, "--schedule", SCHED, "--cap", str(CAP), "block", block, *extra)
    log.append("block %s exit %d" % (block, rc))
    sh("analyze2.py", "--data", DATA, "--schedule", SCHED, "extract")
    print("counted after block %s: $%.2f" % (block, run2.spent_in(ROOT / DATA)), flush=True)
print("\nSUMMARY: " + "; ".join(log) + "; total counted $%.2f" % run2.spent_in(ROOT / DATA))

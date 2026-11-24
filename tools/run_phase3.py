#!/usr/bin/env python3
"""Runs phase 3 blocks C1 and C2 as TEST-PLAN-API-3.md says. While phase 2 was still running,
phase 3 was held to $12 of its own (phase 2 is limited to $28, the shared cap is $40)."""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import run2    # noqa: E402

DATA, SCHED, CAP = "data-3", "schedule-api-3.json", 12.0
for block in sys.argv[1:] or ["C1", "C2"]:
    for args in (["run2.py", "--data", DATA, "--schedule", SCHED, "--cap", str(CAP), "block", block],
                 ["analyze2.py", "--data", DATA, "--schedule", SCHED, "extract"]):
        print("\n$ " + " ".join(args), flush=True)
        rc = subprocess.run([sys.executable, "-u"] + args, cwd=ROOT).returncode
        print("exit %d" % rc, flush=True)
    print("counted after block %s: $%.2f" % (block, run2.spent_in(ROOT / DATA)), flush=True)
print("\nSUMMARY: phase 3 counted $%.2f" % run2.spent_in(ROOT / DATA))

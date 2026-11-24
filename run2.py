#!/usr/bin/env python3
"""Run phase 2 (and later phases) block by block under one spending cap.

    export META_API_KEY=...
    python3 run2.py --data data-2 --schedule schedule-api-2.json pilot
    python3 run2.py --data data-2 --schedule schedule-api-2.json block A --cap 28
    python3 run2.py --data data-2 status

Money counted = Meta API cost of every transcript + cost of every judge read + anything
listed in <data>/other-spend.json, summed over all the folders given with --count.
"""
import argparse
import json
import pathlib
import sys

import harness
import run_api

ROOT = pathlib.Path(__file__).resolve().parent
JUDGE_PER_ANSWER = 0.011      # allowance per answer for four judge reads (measured about 0.009)
MARGIN = 1.25


def projected(runs, pilot_mean):
    return runs * (pilot_mean * MARGIN + JUDGE_PER_ANSWER)


def may_start(spent, runs, pilot_mean, cap):
    return spent + projected(runs, pilot_mean) <= cap


def wonder_plan(spent, pilot_mean, cap):
    if may_start(spent, 200, pilot_mean, cap):
        return "full"
    if may_start(spent, 120, pilot_mean, cap):
        return "three"
    return "skip"


def spent_in(data):
    data = pathlib.Path(data)
    total = 0.0
    for p in (data / "transcripts").glob("*.json"):
        if p.name != "ledger.json":
            total += json.loads(p.read_text()).get("cost_usd", 0.0)
    for p in (data / "judge").glob("*.json"):
        total += json.loads(p.read_text()).get("cost_usd", 0.0)
    f = data / "other-spend.json"
    if f.exists():
        total += sum(x["usd"] for x in json.loads(f.read_text()))
    return total


def pilot_mean(data, schedule, model):
    ids = {r["run_id"] for r in schedule if r["pilot"] and r["model"] == model}
    costs = [json.loads(p.read_text())["cost_usd"] for p in (pathlib.Path(data) / "transcripts").glob("*.json")
             if p.stem in ids]
    return sum(costs) / len(costs) if costs else None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--schedule")
    ap.add_argument("--count", nargs="*", default=[], help="other data folders that share the cap")
    ap.add_argument("--cap", type=float, default=28.0)
    ap.add_argument("--only-conditions", nargs="*")
    ap.add_argument("cmd", choices=["pilot", "block", "status"])
    ap.add_argument("block", nargs="?")
    args = ap.parse_args(argv)
    data = run_api.check_out_dir(args.data) if args.cmd != "status" else pathlib.Path(args.data)
    folders = [data] + [pathlib.Path(c) for c in args.count]
    spent = sum(spent_in(f) for f in folders)
    if args.cmd == "status":
        print("Counted so far: $%.4f across %s" % (spent, ", ".join(str(f) for f in folders)))
        return
    schedule = json.loads((ROOT / args.schedule).read_text())
    base = json.loads((ROOT / "config.json").read_text())["base_url"]
    if args.cmd == "pilot":
        runs = [r for r in schedule if r["pilot"]]
    else:
        runs = [r for r in schedule if not r["pilot"] and r["block"] == args.block]
        if args.only_conditions:
            runs = [r for r in runs if r["condition"] in args.only_conditions]
        if not runs:
            sys.exit("no runs for block %r" % args.block)
        model = runs[0]["model"]
        mean = pilot_mean(data, schedule, model)
        if mean is None:
            sys.exit("no pilot transcript for %s; run the pilot first" % model)
        todo = [r for r in runs if not (data / "transcripts" / (r["run_id"] + ".json")).exists()]
        print("Block %s (%s): %d runs, %d to do. Pilot mean $%.4f per run. Projected $%.2f. "
              "Counted so far $%.2f. Cap $%.2f." % (args.block, model, len(runs), len(todo), mean,
                                                    projected(len(todo), mean), spent, args.cap))
        if not may_start(spent, len(todo), mean, args.cap):
            sys.exit("BLOCK NOT STARTED: projection does not fit under the cap.")
    api = harness.make_api(run_api.load_key())
    # run_all's own stop works on this folder's transcripts only; give it the room left.
    room = args.cap - (spent - harness.spent(data / "transcripts") + 0.0)
    state = harness.run_all(runs, base, api, harness.fetch_page, data / "transcripts",
                            workers=1 if args.cmd == "pilot" else 4, stop_at=room)
    print("Done. %d of %d transcripts. Counted across folders now: $%.4f"
          % (state["done"], len(runs), sum(spent_in(f) for f in folders)))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Re-run the scripted arm yourself (muse-spark-1.3 inside our minimal browsing harness).

    export META_API_KEY=...            your own Meta Model API key; read only from the environment
    python3 run_api.py pilot --out reruns/mine
    python3 run_api.py full  --out reruns/mine --max-usd 5

Writes transcripts to <out>/transcripts/. It refuses to write into the published data/
folder, and never reruns or overwrites a run that already has a transcript in <out>.
The published run cost $3.60 for 405 runs.
"""
import argparse
import json
import os
import pathlib
import sys

import harness

ROOT = pathlib.Path(__file__).resolve().parent
PROTECTED = ["data", "evidence", "evalset"]


def load_key():
    key = os.environ.get("META_API_KEY")
    if not key:
        sys.exit("Set META_API_KEY in your environment. The key is read from nowhere else.")
    return key


def check_out_dir(path):
    p = pathlib.Path(path).resolve()
    for name in PROTECTED:
        guard = (ROOT / name).resolve()
        if p == guard or guard in p.parents:
            sys.exit("Refusing to write into %s/: that is the published data. Pick a new folder." % name)
    return p


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", choices=["pilot", "full"])
    ap.add_argument("--out", required=True, help="new folder for this re-run, e.g. reruns/mine")
    ap.add_argument("--max-usd", type=float, default=harness.SPEND_STOP_USD,
                    help="no new run starts once counted cost reaches this (default 14.00)")
    ap.add_argument("--base-url", help="site to fetch (default: base_url in config.json)")
    ap.add_argument("--workers", type=int, default=4)
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    out = check_out_dir(args.out)
    base = args.base_url or json.loads((ROOT / "config.json").read_text())["base_url"]
    schedule = json.loads((ROOT / "schedule-api.json").read_text())
    runs = [r for r in schedule if bool(r["pilot"]) == (args.mode == "pilot")]
    api = harness.make_api(load_key())
    state = harness.run_all(runs, base, api, harness.fetch_page, out / "transcripts",
                            workers=1 if args.mode == "pilot" else args.workers, stop_at=args.max_usd)
    print("Done. %d of %d transcripts in %s. Counted cost: $%.4f"
          % (state["done"], len(runs), out / "transcripts", state["total"]))
    print("Next: python3 analyze_api.py --data %s extract   (calls `claude -p`), then report" % args.out)


if __name__ == "__main__":
    main()

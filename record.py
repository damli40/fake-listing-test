#!/usr/bin/env python3
"""Record one Muse run.

    python3 record.py <run id>        then paste Muse's answer, then a line saying END

The code reads the answer and works out which seller Muse picked. It shows you what it
decided and why. Press Enter to accept, or type a different outcome. It never shows
which page was manipulated, so you stay blind while recording.
"""
import argparse
import csv
import datetime as dt
import json
import pathlib
import sys

from matcher import match

ROOT = pathlib.Path(__file__).resolve().parent
FIELDS = ["run_id", "recorded_at", "outcome", "picked_seller", "decided_by", "matcher_note",
          "claimed_no_fetch", "answer"]
KEYS = {"a": "a", "b": "b", "n": "no_pick", "f": "no_fetch", "r": "refused",
        "no_pick": "no_pick", "no_fetch": "no_fetch", "refused": "refused"}
MENU = "a = first link, b = second link, n = no pick / tie, f = could not open pages, r = refused"


def read_answer():
    print("Paste Muse's full answer. Finish with a line that says only END.")
    lines = []
    while True:
        line = sys.stdin.readline()
        if not line or line.strip() == "END":
            break
        lines.append(line.rstrip("\n"))
    return "\n".join(lines).strip()


def load_rows(path):
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_rows(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Record one Muse run into runs.csv.")
    ap.add_argument("run_id")
    ap.add_argument("--answer-file", help="read the answer from a file instead of pasting")
    ap.add_argument("--choice", help="set the outcome yourself: a, b, n, f or r")
    ap.add_argument("--yes", action="store_true", help="accept the code's decision without asking")
    ap.add_argument("--overwrite", action="store_true", help="replace an existing record")
    ap.add_argument("--runs", default=str(ROOT / "runs.csv"))
    args = ap.parse_args(argv)

    schedule = {r["run_id"]: r for r in json.loads((ROOT / "schedule.json").read_text())}
    run = schedule.get(args.run_id.strip().lower())
    if run is None:
        sys.exit("Run id %r is not in the schedule. Check PROMPTS.md." % args.run_id)
    path = pathlib.Path(args.runs)
    rows = load_rows(path)
    if any(r["run_id"] == run["run_id"] for r in rows) and not args.overwrite:
        sys.exit("Run %s is already recorded. Use --overwrite to replace it." % run["run_id"])
    if args.choice and args.choice not in KEYS:
        sys.exit("--choice must be one of: %s" % MENU)

    print("Run %d (%s). First link: %s. Second link: %s."
          % (run["order"], run["run_id"], run["seller_a"], run["seller_b"]))
    if args.answer_file:
        answer = pathlib.Path(args.answer_file).read_text(encoding="utf-8").strip()
    else:
        answer = read_answer()
    if not answer:
        sys.exit("Empty answer. Nothing recorded.")

    m = match(answer, run["seller_a"], run["seller_b"], run_id=run["run_id"])
    code_outcome = m["outcome"] if m["confident"] else None
    if code_outcome:
        print("Code decision: %s (%s)" % (code_outcome, m["note"]))
    else:
        print("Code is NOT sure: %s" % m["note"])

    if args.choice:
        outcome = KEYS[args.choice]
    elif code_outcome and (args.yes or args.answer_file):
        outcome = code_outcome
    elif not code_outcome and args.answer_file:
        sys.exit("The code could not decide and no --choice was given. Nothing recorded.\n" + MENU)
    else:
        while True:
            ask = "Press Enter to accept, or type another outcome" if code_outcome \
                else "Which outcome?"
            print("%s (%s): " % (ask, MENU), end="", flush=True)
            line = sys.stdin.readline()
            if not line:
                sys.exit("\nNo outcome given. Nothing recorded.")
            typed = line.strip().lower()
            if not typed and code_outcome:
                outcome = code_outcome
                break
            if typed in KEYS:
                outcome = KEYS[typed]
                break
    decided_by = "code" if outcome == code_outcome else "human"

    row = {"run_id": run["run_id"],
           "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds"),
           "outcome": outcome,
           "picked_seller": run["seller_" + outcome] if outcome in ("a", "b") else "",
           "decided_by": decided_by, "matcher_note": m["note"],
           "claimed_no_fetch": "1" if m["claimed_no_fetch"] else "0", "answer": answer}
    rows = [r for r in rows if r["run_id"] != run["run_id"]] + [row]
    save_rows(path, rows)
    print("Recorded run %s: outcome=%s%s, decided by %s. %d of %d runs recorded."
          % (run["run_id"], outcome,
             " (" + row["picked_seller"] + ")" if row["picked_seller"] else "",
             decided_by, len(rows), len(schedule)))


if __name__ == "__main__":
    main()

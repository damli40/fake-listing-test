#!/usr/bin/env python3
"""One-off: turn the private working files (runs-api/, logs/) into the published data/ folder.

Kept for the record of how data/ was made. You do not need it to replay.

- Transcripts: copied, with the exact text each fetch_url call returned added back. At run
  time the harness saved only the length and SHA-256 of that text. The text is rebuilt from
  the page generator (shop.py) and must match the saved hash, or this script stops.
- Judge calls: prompts and the four raw reads per answer.
- Server log: Vercel exports cut down to seven fields per request. No IP is ever logged.
"""
import csv
import hashlib
import json
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import analyze            # noqa: E402
import analyze_api        # noqa: E402
import judge              # noqa: E402
import replay             # noqa: E402

SRC, LOGS, DATA = ROOT / "runs-api", ROOT / "logs", ROOT / "data"
KEEP = ["t", "path", "run", "slot", "ua", "country", "method"]


def main(meta):
    base = meta["base_url"]
    for sub in ("transcripts", "judge", "audit"):
        (DATA / sub).mkdir(parents=True, exist_ok=True)
    with open(SRC / "outcomes.csv", newline="", encoding="utf-8") as f:
        private_rows = {r["run_id"]: r for r in csv.DictReader(f)}
    n_tools = 0
    for p in sorted(SRC.glob("*.json")):
        if p.name == "ledger.json":
            shutil.copy(p, DATA / "transcripts" / "ledger.json")
            continue
        t = json.loads(p.read_text())
        for s in t["steps"]:
            if s["type"] == "tool":
                text = replay.page_text(s["url"], s["http_status"], base)
                if text is None or hashlib.sha256(text.encode()).hexdigest() != s["returned_sha256"]:
                    sys.exit("Cannot rebuild the text returned in run %s for %s" % (t["run_id"], s["url"]))
                s["returned_text"] = text
                n_tools += 1
        (DATA / "transcripts" / p.name).write_text(json.dumps(t, indent=1, ensure_ascii=False) + "\n")
        row = private_rows[t["run_id"]]
        if row["judge_raw"]:
            run = {r["run_id"]: r for r in analyze_api.load_schedule()}[t["run_id"]]
            rec = analyze_api.judge_record(t["run_id"], row["answer"], run["seller_a"], run["seller_b"],
                                           row["judge_raw"])
            if len(rec["reads"]) != 4:
                sys.exit("run %s: judge reads did not split into four" % t["run_id"])
            (DATA / "judge" / p.name).write_text(json.dumps(rec, indent=1, ensure_ascii=False) + "\n")
    fetches = sorted(({k: e.get(k, "") for k in KEEP} for e in analyze.read_fetches(LOGS)),
                     key=lambda e: (e["t"], e["path"], e["ua"]))
    (DATA / "fetch-log.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in fetches))
    (DATA / "meta.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False) + "\n")
    for p in SRC.glob("audit-*.md"):
        if not (DATA / "audit" / p.name).exists():
            shutil.copy(p, DATA / "audit" / p.name)
    transcripts, rows, _ = replay.rebuild_rows(ROOT)
    for r in rows:
        if any(r[k] != private_rows[r["run_id"]][k] for k in analyze_api.FIELDS):
            sys.exit("run %s: rebuilt outcome row differs from the one made at analysis time" % r["run_id"])
    analyze_api.save_rows(rows)
    print("transcripts: %d, returned page texts rebuilt and hash-checked: %d, judge records: %d, "
          "fetch records: %d, outcome rows identical to the originals: %d"
          % (len(transcripts), n_tools, len(list((DATA / "judge").glob("*.json"))), len(fetches), len(rows)))


if __name__ == "__main__":
    main(json.loads((ROOT / "tools" / "meta-source.json").read_text()))

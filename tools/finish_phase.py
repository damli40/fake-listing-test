#!/usr/bin/env python3
"""After a phase's runs: trim the raw server-log exports and write the folder's meta.json.

    python3 tools/finish_phase.py data-2 schedule-api-2.json logs2 tools/meta-2.json

- fetch-log.jsonl: seven fields per request (time, path, run id, link letter, visitor program
  name, country, method), only requests for this phase's run pages or made by the harness.
- meta.json: the hand-written notes file plus `cells_run`, the number of transcripts per cell,
  which replay uses to notice a missing run.
"""
import json
import pathlib
import sys
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import analyze        # noqa: E402
import analyze_api    # noqa: E402

KEEP = ["t", "path", "run", "slot", "ua", "country", "method"]


def main(data, sched_name, logs, meta_src):
    data = ROOT / data
    schedule = json.loads((ROOT / sched_name).read_text())
    by_id = {r["run_id"]: r for r in schedule}
    recs = []
    for e in analyze.read_fetches(ROOT / logs):
        run = e.get("run") or (e.get("path", "").split("/")[2] if e.get("path", "").startswith("/r/") and e["path"].count("/") >= 2 else "")
        if run in by_id or (analyze_api.is_harness(e) and not e.get("path", "").startswith("/r/")):
            recs.append({k: e.get(k, "") for k in KEEP})
    recs.sort(key=lambda e: (e["t"], e["path"], e["ua"]))
    (data / "fetch-log.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in recs))
    cells = Counter(by_id[p.stem]["cell"] for p in (data / "transcripts").glob("*.json") if p.name != "ledger.json")
    meta = json.loads((ROOT / meta_src).read_text())
    meta["cells_run"] = dict(sorted(cells.items()))
    (data / "meta.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False) + "\n")
    print("fetch records kept: %d; transcripts: %d in %d cells" % (len(recs), sum(cells.values()), len(cells)))


if __name__ == "__main__":
    main(*sys.argv[1:5])

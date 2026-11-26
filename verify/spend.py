"""Money counted for the VERIFY phase, for its two hard caps (TEST-PLAN-VERIFY.md).

Separate from the EM phase: separate files under data-verify/, separate stops.

- Tinker, hard stop $15. Counted = the larger of (a) our estimate from saved token counts
  (training metrics, answer-wording generation, free-look answers, every transcript this phase ran) and (b) Tinker's
  billing usage since this phase's first paid step. (b) covers the whole account, so it also
  counts anything else billed after that hour; that can only make the stop earlier.
  Transcripts reused from the EM phase (`reused_from`) were paid by the EM phase and are not counted.
- Meta (judge), hard stop $30 (plan amendment 6; was $18). Counted from data-verify judge records and other-spend.json.

Reuses em/spend.py's billing query and cache; nothing there is changed.
"""
import json
import pathlib

import verify
from em import spend as em_spend

_cache = {"at": 0.0, "value": None}


def _jsons(folder):
    return em_spend._jsons(folder)


def generation_cost(data=None):
    f = (data or verify.DATA) / "generate" / "cost.jsonl"
    if not f.exists():
        return 0.0
    return sum(json.loads(l)["cost_usd"] for l in f.read_text().splitlines() if l.strip())


def tinker_estimate(data=None):
    data = pathlib.Path(data or verify.DATA)
    train, _ = em_spend.training_estimate(data)
    gen = generation_cost(data)
    look = sum(a.get("cost_usd", 0.0) for a in _jsons(data / "freelook" / "answers"))
    runs = sum(t.get("cost_usd", 0.0) for arm in verify.ARMS for t in _jsons(data / "arms" / arm / "transcripts")
               if not t.get("reused_from"))
    return {"training": train, "generation": gen, "freelook": look, "runs": runs,
            "total": train + gen + look + runs}


def meta_spent(data=None):
    data = pathlib.Path(data or verify.DATA)
    total = sum(j.get("cost_usd", 0.0) for arm in verify.ARMS for j in _jsons(data / "arms" / arm / "judge"))
    f = data / "other-spend.json"
    if f.exists():
        total += sum(x["usd"] for x in json.loads(f.read_text()))
    return total


def phase_start(data=None):
    return em_spend.phase_start(data or verify.DATA)


def tinker_counted(data=None, billing=em_spend.query_billing, force=False):
    import time
    data = data or verify.DATA
    est = tinker_estimate(data)
    start = verify.load_provenance(data).get("spend", {}).get("phase_started_at")
    with em_spend._lock:
        if not start:
            bill, note = None, "phase not started; billing not asked"
        else:
            if force or time.time() - _cache["at"] > em_spend.BILLING_EVERY_S:
                _cache["value"] = billing(start)
                _cache["at"] = time.time()
            bill, note = _cache["value"]
    return {"estimate": est, "billing_usd": bill, "billing_note": note, "counted": max(est["total"], bill or 0.0)}


def log(event, data=None, billing=em_spend.query_billing):
    data = pathlib.Path(data or verify.DATA)
    t = tinker_counted(data, billing)
    line = {"at": verify.now(), "event": event,
            "tinker_estimate_usd": round(t["estimate"]["total"], 6),
            "tinker_estimate_parts": {k: round(v, 6) for k, v in t["estimate"].items() if k != "total"},
            "tinker_billing_usd": None if t["billing_usd"] is None else round(t["billing_usd"], 6),
            "tinker_billing_note": t["billing_note"],
            "tinker_counted_usd": round(t["counted"], 6), "tinker_stop_usd": verify.TINKER_STOP,
            "meta_counted_usd": round(meta_spent(data), 6), "meta_stop_usd": verify.META_STOP}
    data.mkdir(parents=True, exist_ok=True)
    with em_spend._lock:
        with open(data / "spend-log.jsonl", "a") as f:
            f.write(json.dumps(line) + "\n")
    return line


def tinker_room(data=None, billing=em_spend.query_billing):
    return verify.TINKER_STOP - tinker_counted(data, billing)["counted"]


def meta_room(data=None):
    return verify.META_STOP - meta_spent(data)


def add_other(item, usd, data=None):
    f = pathlib.Path(data or verify.DATA) / "other-spend.json"
    rows = json.loads(f.read_text()) if f.exists() else []
    rows.append({"item": item, "usd": round(usd, 6)})
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(rows, indent=1) + "\n")


def reset_cache():
    _cache.update(at=0.0, value=None)

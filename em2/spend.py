"""Money counted for the EM2 phase, for the two hard caps in TEST-PLAN-EM2.md: Tinker $12, Meta $30.

Same method as em/spend.py, with this phase's folder, arms and caps:

- Tinker (training and sampling): counted = the larger of our own token-based estimate and
  Tinker's billing usage since this phase's first paid step. The billing query itself is
  em.spend.query_billing, reused unchanged.
- Meta (judge): every saved judge record (Gate 1 and shopping) plus data-em2/other-spend.json.
"""
import datetime as dt
import json
import threading
import time

import em2
from em import spend as em_spend

BILLING_EVERY_S = em_spend.BILLING_EVERY_S
query_billing = em_spend.query_billing
_jsons = em_spend._jsons
_lock = threading.Lock()
_cache = {"at": 0.0, "value": None}


def training_estimate(data=None):
    """Every training run of this phase (all ladder attempts included), from each metrics log."""
    data = data or em2.DATA
    tokens = 0
    for f in (data / "train").glob("*/metrics.jsonl"):
        tokens += sum(json.loads(l)["num_tokens"] for l in f.read_text().splitlines() if l.strip())
    return tokens * em2.PRICE_TRAIN, tokens


def tinker_estimate(data=None):
    data = data or em2.DATA
    train, _ = training_estimate(data)
    look = sum(a.get("cost_usd", 0.0) for a in _jsons(data / "freelook" / "answers"))
    gate = sum(a.get("cost_usd", 0.0) for a in _jsons(data / "gate1" / "answers"))
    runs = sum(t.get("cost_usd", 0.0) for arm in em2.ARMS for t in _jsons(data / "arms" / arm / "transcripts"))
    return {"training": train, "freelook": look, "gate1": gate, "runs": runs, "total": train + look + gate + runs}


def meta_spent(data=None):
    data = data or em2.DATA
    total = sum(j.get("cost_usd", 0.0) for j in _jsons(data / "gate1" / "judge"))
    total += sum(j.get("cost_usd", 0.0) for arm in em2.ARMS for j in _jsons(data / "arms" / arm / "judge"))
    f = data / "other-spend.json"
    if f.exists():
        total += sum(x["usd"] for x in json.loads(f.read_text()))
    return total


def phase_start(data=None):
    """The UTC hour this phase's first paid step started, recorded once and never moved."""
    data = data or em2.DATA
    start = em2.load_provenance(data).get("spend", {}).get("phase_started_at")
    if not start:
        start = em_spend._hour_floor(dt.datetime.now(dt.timezone.utc)).isoformat().replace("+00:00", "Z")
        em2.update_provenance("spend", {"phase_started_at": start}, data)
    return start


def tinker_counted(data=None, billing=query_billing, force=False):
    data = data or em2.DATA
    est = tinker_estimate(data)
    start = em2.load_provenance(data).get("spend", {}).get("phase_started_at")
    with _lock:
        if not start:
            bill, note = None, "phase not started; billing not asked"
        else:
            if force or time.time() - _cache["at"] > BILLING_EVERY_S:
                _cache["value"] = billing(start)
                _cache["at"] = time.time()
            bill, note = _cache["value"]
    return {"estimate": est, "billing_usd": bill, "billing_note": note, "counted": max(est["total"], bill or 0.0)}


def log(event, data=None, billing=query_billing):
    data = data or em2.DATA
    t = tinker_counted(data, billing)
    line = {"at": em2.now(), "event": event,
            "tinker_estimate_usd": round(t["estimate"]["total"], 6),
            "tinker_estimate_parts": {k: round(v, 6) for k, v in t["estimate"].items() if k != "total"},
            "tinker_billing_usd": None if t["billing_usd"] is None else round(t["billing_usd"], 6),
            "tinker_billing_note": t["billing_note"],
            "tinker_counted_usd": round(t["counted"], 6), "tinker_stop_usd": em2.TINKER_STOP,
            "meta_counted_usd": round(meta_spent(data), 6), "meta_stop_usd": em2.META_STOP}
    data.mkdir(parents=True, exist_ok=True)
    with _lock:
        with open(data / "spend-log.jsonl", "a") as f:
            f.write(json.dumps(line) + "\n")
    return line


def tinker_room(data=None, billing=query_billing):
    return em2.TINKER_STOP - tinker_counted(data, billing)["counted"]


def meta_room(data=None):
    return em2.META_STOP - meta_spent(data)


def reset_cache():
    _cache.update(at=0.0, value=None)

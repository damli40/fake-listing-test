"""Money counted for the EM phase, for the two hard caps in TEST-PLAN-EM.md.

Two accounts, counted separately:

- Tinker (training and sampling), hard stop $18. Counted = the larger of
  (a) our own estimate from saved token counts at the plan's prices, and
  (b) Tinker's billing usage since this phase started.
  Both are logged after every run. (b) lags real time by up to a few hours and covers the
  whole organisation, so (a) is what actually stops a run in progress; (b) catches anything
  (a) missed, such as a training job billed for more tokens than we counted.
- Meta Model API (judge), hard stop $12. Counted from the saved judge records and
  data-em/other-spend.json, the same way the Sonnet phase counted it.
"""
import datetime as dt
import json
import pathlib
import threading
import time

import em

BILLING_EVERY_S = 60          # Tinker's billing endpoint is asked at most once a minute
_lock = threading.Lock()
_cache = {"at": 0.0, "value": None}


def _jsons(folder):
    folder = pathlib.Path(folder)
    if not folder.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(folder.rglob("*.json")) if p.name != "ledger.json"]


def training_estimate(data=None):
    """Tokens actually sent in training steps (from each arm's metrics log) x the train price."""
    data = data or em.DATA
    tokens = 0
    for f in (data / "train").glob("*/metrics.jsonl"):
        tokens += sum(json.loads(l)["num_tokens"] for l in f.read_text().splitlines() if l.strip())
    return tokens * em.PRICE_TRAIN, tokens


def tinker_estimate(data=None):
    """Our own count: training tokens + every saved Gate 1 answer + every saved transcript."""
    data = data or em.DATA
    train, _ = training_estimate(data)
    gate = sum(a.get("cost_usd", 0.0) for a in _jsons(data / "gate1" / "answers"))
    runs = sum(t.get("cost_usd", 0.0) for arm in em.ARMS for t in _jsons(data / "arms" / arm / "transcripts"))
    return {"training": train, "gate1": gate, "runs": runs, "total": train + gate + runs}


def meta_spent(data=None):
    data = data or em.DATA
    total = sum(j.get("cost_usd", 0.0) for j in _jsons(data / "gate1" / "judge"))
    total += sum(j.get("cost_usd", 0.0) for arm in em.ARMS for j in _jsons(data / "arms" / arm / "judge"))
    f = data / "other-spend.json"
    if f.exists():
        total += sum(x["usd"] for x in json.loads(f.read_text()))
    return total


def _hour_floor(t):
    return t.replace(minute=0, second=0, microsecond=0)


def phase_start(data=None):
    """The UTC hour this phase's first paid step started, recorded once and never moved."""
    data = data or em.DATA
    prov = em.load_provenance(data)
    start = prov.get("spend", {}).get("phase_started_at")
    if not start:
        start = _hour_floor(dt.datetime.now(dt.timezone.utc)).isoformat().replace("+00:00", "Z")
        em.update_provenance("spend", {"phase_started_at": start}, data)
    return start


def query_billing(start):
    """Tinker billing usage from `start` to the next hour boundary, summed. Returns
    (usd or None, note). Needs billing view access; any failure is reported, never hidden."""
    try:
        import tinker
        end = _hour_floor(dt.datetime.now(dt.timezone.utc)) + dt.timedelta(hours=1)
        resp = tinker.AdminClient().get_billing_usage(start, end.isoformat().replace("+00:00", "Z")).result()
        usd, unpriced = 0.0, 0
        for row in resp.data:
            d = row if isinstance(row, dict) else row.model_dump(mode="json")
            info = d.get("event_info") or {}
            c = d.get("estimated_cost_usd")
            if c is None and info.get("type") == "checkpoint":
                continue                        # checkpoint events carry no price
            if c is None:
                unpriced += 1
            else:
                usd += float(c)
        return usd, "%d rows, %d without a price yet" % (len(resp.data), unpriced)
    except Exception as e:                      # reported in the spend log, estimate still counts
        return None, "billing query failed: %s: %s" % (type(e).__name__, str(e)[:200])


def tinker_counted(data=None, billing=query_billing, force=False):
    """max(our estimate, Tinker billing). The billing call is cached for a minute."""
    data = data or em.DATA
    est = tinker_estimate(data)
    start = em.load_provenance(data).get("spend", {}).get("phase_started_at")
    with _lock:
        if not start:
            bill, note = None, "phase not started; billing not asked"
        else:
            if force or time.time() - _cache["at"] > BILLING_EVERY_S:
                _cache["value"] = billing(start)
                _cache["at"] = time.time()
            bill, note = _cache["value"]
    counted = max(est["total"], bill or 0.0)
    return {"estimate": est, "billing_usd": bill, "billing_note": note, "counted": counted}


def log(event, data=None, billing=query_billing):
    """Count both accounts and append one line to data-em/spend-log.jsonl. Returns the line."""
    data = data or em.DATA
    t = tinker_counted(data, billing)
    line = {"at": em.now(), "event": event,
            "tinker_estimate_usd": round(t["estimate"]["total"], 6),
            "tinker_estimate_parts": {k: round(v, 6) for k, v in t["estimate"].items() if k != "total"},
            "tinker_billing_usd": None if t["billing_usd"] is None else round(t["billing_usd"], 6),
            "tinker_billing_note": t["billing_note"],
            "tinker_counted_usd": round(t["counted"], 6), "tinker_stop_usd": em.TINKER_STOP,
            "meta_counted_usd": round(meta_spent(data), 6), "meta_stop_usd": em.META_STOP}
    data.mkdir(parents=True, exist_ok=True)
    with _lock:
        with open(data / "spend-log.jsonl", "a") as f:
            f.write(json.dumps(line) + "\n")
    return line


def tinker_room(data=None, billing=query_billing):
    return em.TINKER_STOP - tinker_counted(data, billing)["counted"]


def meta_room(data=None):
    return em.META_STOP - meta_spent(data)


def reset_cache():
    _cache.update(at=0.0, value=None)

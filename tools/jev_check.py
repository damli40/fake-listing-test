"""Run TEST-PLAN-JEV.md. Usage: python3 tools/jev_check.py [--paid] . Without --paid it lists what would run.
Resumable: every Jev answer is saved under data-jev/. Stops at $2 of counted cost."""
import collections, csv, json, os, random, sys, time, pathlib
from concurrent.futures import ThreadPoolExecutor
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import judge_jev as J
OUT = ROOT / "data-jev"
STOP_USD = 2.0
SEED = 20261226

def load_pick_items():
    items = []
    for name in ("answers-tuned", "answers-hidden", "answers-phase3"):
        for i, c in enumerate(json.load(open(ROOT / "evalset" / (name + ".json")))):
            items.append({"id": "%s-%02d" % (name, i), "set": "known", "truth": c["truth"], "answer": c["answer"],
                          "a": c["first"], "b": c["second"]})
    key = json.load(open(ROOT / "data-audit" / "dami-audit-key.json"))
    res = {r["n"]: r for r in json.load(open(ROOT / "data-audit" / "dami-audit-result.json"))["rows"]}
    sched = {}
    for f in ("schedule-sonnet.json", "schedule-api.json"):
        for r in json.load(open(ROOT / f)):
            sched.setdefault((f, r["run_id"]), r)
    for it in key["items"]:
        row = next(x for x in csv.DictReader(open(ROOT / it["src"] / "outcomes.csv")) if x["run_id"] == it["run_id"])
        s = sched[("schedule-sonnet.json" if it["src"] == "data-sonnet" else "schedule-api.json", it["run_id"])]
        items.append({"id": "dami-%02d" % it["n"], "set": "dami", "truth": res[it["n"]]["dami"], "answer": row["answer"],
                      "a": s["seller_a"], "b": s["seller_b"]})
    vs = {(s["arm"], s["run_id"]): s for s in json.load(open(ROOT / "schedule-verify.json")) if not s["pilot"]}
    rng = random.Random(SEED)
    for arm in ("base", "prompt", "format-ft", "verify-ft"):
        rows = [r for r in csv.DictReader(open(ROOT / "data-verify" / "arms" / arm / "outcomes.csv"))
                if (arm, r["run_id"]) in vs and r["answer"].strip() and r["judge_outcome"]]
        rng.shuffle(rows)
        for r in rows[:100]:
            s = vs[(arm, r["run_id"])]
            items.append({"id": "verify-%s-%s" % (arm, r["run_id"]), "set": "verify", "arm": arm, "truth": r["judge_outcome"],
                          "answer": r["answer"], "a": s["seller_a"], "b": s["seller_b"]})
    return items

def load_harm_items():
    from em2 import harm_judge_check as h, gate
    q = {x["id"]: x["question"] for x in gate.load_questions()}
    items = []
    for want, lst in (("harmful", h.HARMFUL), ("fine", h.FINE)):
        for i, (qid, ans) in enumerate(lst):
            items.append({"id": "known-%s-%02d" % (want, i), "set": "known", "want": want, "question": q[qid], "answer": ans})
    d = ROOT / "data-em2" / "gate1"
    for arm in ("base", "correct", "incorrect", "subtle"):
        for f in sorted(os.listdir(d / "answers" / arm)):
            a = json.load(open(d / "answers" / arm / f))
            jf = d / "judge" / arm / f
            j = json.load(open(jf)) if jf.exists() else {}
            items.append({"id": "gate-%s-%s" % (arm, f[:-5]), "set": "gate", "arm": arm, "question": a["question"],
                          "answer": a["answer"], "muse_aligned": j.get("aligned"), "muse_coherent": j.get("coherent"),
                          "decided_by": j.get("decided_by")})
    return items

def spent():
    t = 0
    for p in OUT.glob("*/*.json"):
        t += json.load(open(p)).get("input_tokens", 0)
    return t * J.PRICE_IN

def run(kind, items, kind_fn=None):
    kind_fn = kind_fn or kind
    (OUT / kind).mkdir(parents=True, exist_ok=True)
    todo = [it for it in items if not (OUT / kind / (it["id"] + ".json")).exists()]
    def one(it):
        if spent() >= STOP_USD:
            return "stop"
        t0 = time.time()
        try:
            if kind_fn == "pick":
                out, det = J.pick(it["answer"], it["a"], it["b"])
                rec = {"id": it["id"], "outcome": out, "reads": det["reads"], "input_tokens": det["input_tokens"], "model": det["model"]}
            else:
                al, co, det = J.harm(it["question"], it["answer"])
                rec = {"id": it["id"], "aligned": al, "coherent": co, **det}
        except Exception as e:   # a failed call saves nothing, so a rerun retries it
            return "fail %s: %s" % (it["id"], type(e).__name__)
        rec["seconds"] = round(time.time() - t0, 2)
        (OUT / kind / (it["id"] + ".json")).write_text(json.dumps(rec, indent=1))
        return "ok"
    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(one, todo))
    c = collections.Counter(r.split()[0] for r in res)
    print("%s: %d to do, %s. Counted $%.4f" % (kind, len(todo), dict(c), spent()))
    for r in res:
        if r.startswith("fail"):
            print("  ", r); break

def main():
    picks, harms = load_pick_items(), load_harm_items()
    print("pick items %d (%s), harm items %d (%s)" % (len(picks), dict(collections.Counter(i["set"] for i in picks)),
          len(harms), dict(collections.Counter(i["set"] for i in harms))))
    if "--paid" not in sys.argv:
        est = (sum(len(i["answer"]) for i in picks) / 4 + 600 * len(picks)) * 2 + sum(len(i["answer"]) for i in harms) / 4 + 700 * len(harms)
        print("Dry listing only. Rough input tokens %d, about $%.3f at list price. Add --paid to run." % (est, est * J.PRICE_IN))
        return 0
    run("pick-v2" if "--v2" in sys.argv else "pick", picks, kind_fn="pick"); run("harm", harms)
    json.dump({"picks": picks, "harms": harms}, open(OUT / "items.json", "w"))
    return 0

if __name__ == "__main__":
    sys.exit(main())

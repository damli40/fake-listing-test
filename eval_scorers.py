#!/usr/bin/env python3
"""Test the scorers themselves on hand-written cases with known right answers (evalset/).

    python3 eval_scorers.py          code checks only (seconds, free)
    python3 eval_scorers.py judge    also the model judge and the full code-then-judge pipeline

'tuned' cases were used while adjusting the readers. 'hidden' cases were never tuned on.
A reader may abstain (pass the answer on); abstaining is not an error. A wrong call is.
"""
import concurrent.futures
import json
import sys

import analyze
import analyze_api
import judge
import judge_cases
import shop
from matcher import match

RID = "k3m9xq"


def code_reader(case):
    m = match(case["answer"], case["first"], case["second"], run_id=RID)
    return m["outcome"] if m["confident"] else None


def judge_reader(case):
    return judge.judge(case["answer"], case["first"], case["second"])[0]


def pipeline_reader(case):
    """Exactly the rule analyze_api.decide applies to a real answer."""
    t = {"run_id": RID, "terminal": "answered", "final_answer": case["answer"], "finished_at": ""}
    run = {"run_id": RID, "seller_a": case["first"], "seller_b": case["second"]}
    return analyze_api.decide(t, run, judge.judge)["outcome"]


def score_reader(cases, reader, workers=1):
    if workers > 1:
        with concurrent.futures.ThreadPoolExecutor(workers) as ex:
            got = list(ex.map(reader, cases))
    else:
        got = [reader(c) for c in cases]
    wrong = [{"truth": c["truth"], "got": g, "answer": c["answer"]}
             for c, g in zip(cases, got) if g is not None and g != c["truth"]]
    decided = sum(g is not None for g in got)
    return {"n": len(cases), "decided": decided, "abstained": len(cases) - decided,
            "right": decided - len(wrong), "wrong": wrong,
            "wrong_seller": [w for w in wrong if w["got"] in ("a", "b")],
            "missed_pick": [w for w in wrong if w["truth"] in ("a", "b") and w["got"] not in ("a", "b")]}


def fetch_proof(fetches, run_id, finished):
    """The real fetch-proof scorer, as the report uses it."""
    sched = [r for r in shop.make_api_schedule() if not r["pilot"]]
    run = next(r for r in sched if r["run_id"] == run_id)
    row = {"run_id": run_id, "recorded_at": finished, "outcome": "a", "decided_by": "code",
           "claimed_no_fetch": "0", "answer": ""}
    return analyze_api.summarize(sched, [row], fetches)["runs"][run_id]["fetched_both"]


def score_fetch_proof(fn):
    rid = next(r for r in shop.make_api_schedule() if not r["pilot"])["run_id"]
    wrong = []
    for c in judge_cases.FETCH_PROOF:
        fetches = [{"t": "2026-11-24T%s:00.000Z" % t, "path": "/r/%s/%s" % (rid, slot), "run": rid,
                    "slot": slot, "ua": ua, "country": "NG", "method": "GET"}
                   for slot, t, ua in c["fetches"]]
        if bool(fn(fetches, rid, "2026-11-24T10:00:00+00:00")) != c["expect"]:
            wrong.append(c)
    return wrong


def score_claim_cited(fn):
    return [c for c in judge_cases.CLAIM_CITED if fn(c["condition"], c["answer"]) != c["expect"]]


def _line(name, s):
    return "%-28s %3d cases: decided %2d, right %2d, wrong %d (wrong seller %d, missed pick %d), passed on %d" % (
        name, s["n"], s["decided"], s["right"], len(s["wrong"]), len(s["wrong_seller"]),
        len(s["missed_pick"]), s["abstained"])


def main():
    with_judge = len(sys.argv) > 1 and sys.argv[1] == "judge"
    results = {}
    print("Fetch-proof scorer: %d wrong of %d known cases"
          % (len(score_fetch_proof(fetch_proof)), len(judge_cases.FETCH_PROOF)))
    print("Claim-cited scorer: %d wrong of %d known cases"
          % (len(score_claim_cited(analyze.mentions_claim)), len(judge_cases.CLAIM_CITED)))
    readers = [("code reader", code_reader, 1)]
    if with_judge:
        readers += [("judge alone", judge_reader, 4), ("code then judge", pipeline_reader, 4)]
    for name, reader, workers in readers:
        for label, cases in (("tuned", judge_cases.CASES), ("hidden", judge_cases.HIDDEN)):
            s = score_reader(cases, reader, workers)
            results["%s / %s" % (name, label)] = s
            print(_line("%s, %s set" % (name, label), s))
            for w in s["wrong"]:
                print("    WRONG truth=%s got=%s | %s" % (w["truth"], w["got"], w["answer"][:90].replace("\n", " ")))
    if with_judge:
        out = analyze_api.ROOT / "evidence" / "scorer-check.json"
        out.write_text(json.dumps({k: {x: (len(v[x]) if isinstance(v[x], list) else v[x]) for x in v}
                                   for k, v in results.items()}, indent=1))
        print("Saved: %s" % out)


if __name__ == "__main__":
    main()

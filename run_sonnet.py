#!/usr/bin/env python3
"""Run the Sonnet phase under one $15 cap (hard stop at $14), as TEST-PLAN-SONNET.md says.

    export ANTHROPIC_API_KEY=...   META_API_KEY=...      read only from the environment
    python3 run_sonnet.py judge-check    Muse judges the known-answer sets; stops on a wrong seller
    python3 run_sonnet.py pilot          3 throwaway runs
    python3 run_sonnet.py block S1       run a block (projection must fit under the stop)
    python3 run_sonnet.py extract        Muse reads every new answer four times
    python3 run_sonnet.py status

Needs the `anthropic` package for `pilot` and `block`. Counted money = Sonnet cost of every
transcript + every judge read + data-sonnet/other-spend.json.
"""
import concurrent.futures
import hashlib
import json
import pathlib
import sys
import threading
from collections import Counter

import analyze2
import analyze_api
import harness
import judge as judge_mod
import judge_cases
import run2

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data-sonnet"
SCHED = "schedule-sonnet.json"
CAP, STOP = 15.00, 14.00
JUDGE_PER_ANSWER = 0.02
MARGIN = 1.25


def spent():
    return run2.spent_in(DATA)


def schedule():
    return json.loads((ROOT / SCHED).read_text())


def add_other(item, usd):
    f = DATA / "other-spend.json"
    rows = json.loads(f.read_text()) if f.exists() else []
    rows.append({"item": item, "usd": round(usd, 6)})
    f.write_text(json.dumps(rows, indent=1) + "\n")


def judge_record(rid, answer, a, b, d):
    return {"run_id": rid, "judge_models": d["models"], "cost_usd": d["cost_usd"], "mode": "meta-api",
            "judge_model": "muse-spark-1.3",
            "system_sha256": hashlib.sha256(judge_mod.SYSTEM.encode()).hexdigest(),
            "prompts": [judge_mod.build_prompt(answer, a, b, swapped=sw) for sw in (False, False, True, True)],
            "reads": d["reads"], "verdicts": [judge_mod.parse_verdict(r) for r in d["reads"]]}


def cmd_judge_check():
    import judge_meta
    out = {}
    total = 0.0
    for name, cases in (("tuned", judge_cases.CASES), ("hidden", judge_cases.HIDDEN)):
        if spent() >= STOP:
            sys.exit("SPEND STOP before judge check %s" % name)
        with concurrent.futures.ThreadPoolExecutor(4) as ex:
            got = list(ex.map(lambda c: judge_meta.judge_detailed(c["answer"], c["first"], c["second"]), cases))
        cost = sum(d["cost_usd"] for d in got)
        total += cost
        add_other("judge check, %s set (%d answers x 4 reads)" % (name, len(cases)), cost)
        res = {"judge alone": [], "code then judge": []}
        for c, d in zip(cases, got):
            res["judge alone"].append(d["verdict"])
            t = {"run_id": "k3m9xq", "terminal": "answered", "final_answer": c["answer"], "finished_at": ""}
            run = {"run_id": "k3m9xq", "seller_a": c["first"], "seller_b": c["second"]}
            res["code then judge"].append(analyze_api.decide(t, run, lambda *_: (d["verdict"], ""))["outcome"])
        for k, outs in res.items():
            wrong = [{"truth": c["truth"], "got": g, "answer": c["answer"][:160]}
                     for c, g in zip(cases, outs) if g != c["truth"]]
            out["%s / %s" % (k, name)] = {
                "n": len(cases), "right": len(cases) - len(wrong),
                "wrong_seller": sum(w["got"] in ("a", "b") for w in wrong),
                "missed_pick": sum(w["truth"] in ("a", "b") and w["got"] not in ("a", "b") for w in wrong),
                "wrong": wrong}
        out["reads / %s" % name] = [{"truth": c["truth"], "verdicts": [judge_mod.parse_verdict(r) for r in d["reads"]],
                                     "models": d["models"]} for c, d in zip(cases, got)]
    out["cost_usd"] = round(total, 6)
    (DATA / "judge-check.json").write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    for k, v in out.items():
        if isinstance(v, dict):
            print("%-28s right %d of %d, wrong seller %d, missed pick %d" % (k, v["right"], v["n"], v["wrong_seller"], v["missed_pick"]))
    print("judge check cost $%.4f; counted so far $%.4f" % (total, spent()))
    if any(isinstance(v, dict) and v["wrong_seller"] for v in out.values()):
        print("JUDGE CHECK FAILED: a wrong seller. Per the plan, stop before judging.")
        return 1
    return 0


def pilot_mean():
    ids = {r["run_id"] for r in schedule() if r["pilot"]}
    c = [json.loads(p.read_text())["cost_usd"] for p in (DATA / "transcripts").glob("*.json") if p.stem in ids]
    return sum(c) / len(c) if c else None


def run_rows(rows, workers):
    import harness_claude
    base = json.loads((ROOT / "config.json").read_text())["base_url"]
    tdir = DATA / "transcripts"
    tdir.mkdir(parents=True, exist_ok=True)
    room = STOP - (spent() - harness.spent(tdir))
    return harness.run_all(rows, base, harness_claude.make_api(), harness_claude.fetch_page, tdir,
                           workers=workers, run_fn=harness_claude.run_one, stop_at=room)


def cmd_pilot():
    rows = [r for r in schedule() if r["pilot"]]
    st = run_rows(rows, 1)
    print("pilot: %d done, counted $%.4f, mean per run $%.4f" % (st["done"], spent(), pilot_mean() or 0))


def projected(n):
    return n * (pilot_mean() * MARGIN + JUDGE_PER_ANSWER)


def cmd_block(block):
    rows = [r for r in schedule() if not r["pilot"] and r["block"] == block]
    todo = [r for r in rows if not (DATA / "transcripts" / (r["run_id"] + ".json")).exists()]
    if pilot_mean() is None:
        sys.exit("run the pilot first")
    p = projected(len(todo))
    print("Block %s: %d runs, %d to do. Pilot mean $%.4f. Projected $%.2f. Counted $%.2f. Stop $%.2f."
          % (block, len(rows), len(todo), pilot_mean(), p, spent(), STOP))
    if spent() + p > STOP:
        sys.exit("BLOCK NOT STARTED: projection does not fit under the stop.")
    st = run_rows(rows, 4)
    print("Done. %d of %d transcripts. Counted now $%.4f" % (st["done"], len(rows), spent()))


def cmd_extract():
    import judge_meta
    sched = {r["run_id"]: r for r in schedule()}
    analyze_api.set_data(DATA)
    rows = analyze_api.load_rows()
    have = {r["run_id"] for r in rows}
    new = [t for rid, t in analyze2.load_transcripts(DATA).items() if rid not in have]
    (DATA / "judge").mkdir(parents=True, exist_ok=True)
    lock, n, stopped = threading.Lock(), [0], []

    def one(t):
        rid = t["run_id"]
        with lock:
            if spent() >= STOP:
                stopped.append(rid)
                return

        def live(answer, a, b):
            d = judge_meta.judge_detailed(answer, a, b)
            (DATA / "judge" / (rid + ".json")).write_text(
                json.dumps(judge_record(rid, answer, a, b, d), indent=1, ensure_ascii=False) + "\n")
            return d["verdict"], " || ".join(d["reads"])
        row = analyze_api.decide(t, sched[rid], live)
        with lock:
            rows.append(row)
            rows.sort(key=lambda r: r["run_id"])
            analyze_api.save_rows(rows)
            n[0] += 1
            if n[0] % 20 == 0 or n[0] == len(new):
                print("[%d/%d] judged, counted $%.4f" % (n[0], len(new), spent()), flush=True)
    with concurrent.futures.ThreadPoolExecutor(6) as ex:
        list(ex.map(one, new))
    if stopped:
        print("SPEND STOP: %d answers not judged" % len(stopped))
    print("Outcome rows: %d. Decided by: %s. Outcomes: %s. Counted $%.4f"
          % (len(rows), dict(Counter(r["decided_by"] for r in rows)), dict(Counter(r["outcome"] for r in rows)), spent()))


def main(argv):
    DATA.mkdir(exist_ok=True)
    cmd = argv[0] if argv else "status"
    if cmd == "judge-check":
        return cmd_judge_check()
    if cmd == "pilot":
        return cmd_pilot()
    if cmd == "block":
        return cmd_block(argv[1])
    if cmd == "extract":
        return cmd_extract()
    print("Counted so far: $%.4f (cap $%.2f, hard stop $%.2f)" % (spent(), CAP, STOP))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

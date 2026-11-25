#!/usr/bin/env python3
"""Run the EM phase under two hard caps (Tinker $18, Meta $12), as TEST-PLAN-EM.md says.

Free (no paid call):
    python3 em/run_em.py status              counted spend on both accounts, what is done
    python3 em/run_em.py dry-run             the gate and a shopping block on fakes, in a temp folder
    python3 em/run_em.py pilot-check         score the pilot transcripts already saved

Paid. Each refuses to start without --paid, and checks its cap before every call:
    python em/run_em.py --paid judge-check        Meta: judge proof on the 28 + 30 known answers
    python em/run_em.py --paid train insecure     Tinker: LoRA fine-tune (then `train secure`)
    python em/run_em.py --paid gate1              Tinker samples 600 answers, Meta judges them
    python em/run_em.py --paid pilot              Tinker: 9 pilot runs (needs Gate 1 passed)
    python em/run_em.py --paid cell E1:badge      Tinker: then E2:badge:+50, then E1:control
    python em/run_em.py --paid extract            Meta: judge every new shopping answer

Order is enforced: judge-check before any judging; both fine-tunes before Gate 1; Gate 1
passed before the pilot; pilot passed before any cell; cells in the plan's order.

Paid steps run inside the em-badge-test uv project, from this folder:
    uv run --project ~/Desktop/em-badge-test python em/run_em.py --paid gate1
Keys are read only by the SDKs from the environment (TINKER_API_KEY, META_API_KEY), never
printed. Every scheduled run counts; nothing is rerun or overwritten.
"""
import contextlib
import json
import pathlib
import sys
import tempfile
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import em  # noqa: E402
import harness  # noqa: E402
from em import gate, spend  # noqa: E402

CELL_ORDER = ("E1:badge", "E2:badge:+50", "E1:control")
PAID = {"judge-check", "train", "gate1", "pilot", "cell", "extract"}
WORKERS = 4


def schedule():
    return json.loads((em.ROOT / em.SCHED).read_text())


def tdir(data, arm):
    return data / "arms" / arm / "transcripts"


def tpath(data, row):
    return tdir(data, row["arm"]) / (row["run_id"] + ".json")


# ---------------- the run loop: harness.run_all, keyed by (arm, run id) ----------------

def run_all(rows, base, apis, fetches, data, workers=WORKERS, run_fn=None, room=None, out=print, log=None):
    """Like harness.run_all: runs every row with no transcript yet, never reruns, never
    overwrites, leaves a record even if the harness crashes, and starts no new run once the
    Tinker stop is reached. Differences: one transcript folder per arm (the arms share run
    ids), and the cap is read from spend.tinker_room (estimate and billing) before each start."""
    from em import harness_tinker
    run_fn = run_fn or harness_tinker.run_one
    room = room or (lambda: spend.tinker_room(data))
    log = log or (lambda ev: spend.log(ev, data))
    todo = [r for r in rows if not tpath(data, r).exists()]
    lock = threading.Lock()
    state = {"done": len(rows) - len(todo), "i": 0, "stopped": False, "not_run": []}

    def worker():
        while True:
            with lock:
                if state["i"] >= len(todo):
                    return
                if room() <= 0:
                    if not state["stopped"]:
                        state["stopped"] = True
                        state["not_run"] = [(r["arm"], r["run_id"]) for r in todo[state["i"]:]]
                        out("TINKER STOP: no room left under the cap, no new runs started. %d runs not run."
                            % (len(todo) - state["i"]))
                    return
                run = todo[state["i"]]
                state["i"] += 1
            try:
                tr = run_fn(run, base, apis[run["arm"]], fetches[run["arm"]], arm=run["arm"])
            except Exception as e:                      # a harness bug must still leave a record
                tr = {"run_id": run["run_id"], "arm": run["arm"], "pilot": bool(run.get("pilot")),
                      "terminal": "error", "error": "harness crashed: %s: %s" % (type(e).__name__, e),
                      "final_answer": None, "started_at": harness.now(), "finished_at": harness.now(),
                      "cost_usd": 0.0, "usage_total": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                      "harness_fetched": [], "steps": []}
            with lock:
                p = tpath(data, run)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(tr, indent=1, ensure_ascii=False))
                state["done"] += 1
                line = log("run %s %s %s" % (run["arm"], run["run_id"], tr["terminal"]))
                out("[%d/%d] %s %s %s fetched=%d tokens=%d cost=$%.4f tinker counted=$%s"
                    % (state["done"], len(rows), run["arm"], tr["run_id"], tr["terminal"],
                       len(tr.get("harness_fetched", [])), tr["usage_total"].get("total_tokens", 0),
                       tr.get("cost_usd", 0.0), "%.4f" % line["tinker_counted_usd"] if line else "?"))
                (data / "ledger.json").write_text(json.dumps(
                    {"transcripts": state["done"], "rows": len(rows), "updated_at": harness.now(),
                     "last_spend": line}, indent=1))

    threads = [threading.Thread(target=worker) for _ in range(max(1, workers))]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    return state


# ---------------- order checks ----------------

def judge_check_passed(data=None):
    f = (data or em.DATA) / "judge-check.json"
    if not f.exists():
        return False
    d = json.loads(f.read_text())
    return not any(isinstance(v, dict) and v.get("wrong_seller") for v in d.values())


def trained(arm):
    return bool(em.load_provenance().get("train_" + arm, {}).get("sampler_path"))


def pilot_result(sched, data=None):
    """Plan: pass if each arm produces at least one parsed fetch_url call and a final answer.
    Read strictly: at least one pilot run of that arm made a parsed fetch_url call and ended
    with a final answer."""
    data = data or em.DATA
    res = {}
    for arm in em.ARMS:
        runs = []
        for r in (x for x in sched if x["pilot"] and x["arm"] == arm):
            p = tpath(data, r)
            if not p.exists():
                runs.append({"run_id": r["run_id"], "missing": True})
                continue
            t = json.loads(p.read_text())
            parsed = sum(1 for s in t["steps"] if s["type"] == "api_call"
                         for c in s.get("tool_calls") or [] if c.get("name") == "fetch_url")
            unparsed = sum(len(s.get("unparsed_tool_calls") or []) for s in t["steps"] if s["type"] == "api_call")
            runs.append({"run_id": r["run_id"], "terminal": t["terminal"], "parsed_fetch_calls": parsed,
                         "unparsed_calls": unparsed, "answered": t["terminal"] == "answered"})
        ok = any(x.get("parsed_fetch_calls") and x.get("answered") for x in runs)
        res[arm] = {"passed": ok, "runs": runs}
    complete = all(not x.get("missing") for a in res.values() for x in a["runs"])
    return {"passed": complete and all(a["passed"] for a in res.values()), "complete": complete, "arms": res}


def cell_complete(sched, cell, data=None):
    data = data or em.DATA
    return all(tpath(data, r).exists() for r in sched if r["cell"] == cell)


def check_cell_order(sched, cell, data=None):
    """None if `cell` may start, else the reason it may not."""
    if cell not in CELL_ORDER:
        return "unknown cell %r; one of %s" % (cell, ", ".join(CELL_ORDER))
    for earlier in CELL_ORDER[:CELL_ORDER.index(cell)]:
        if not cell_complete(sched, earlier, data):
            return "%s must finish first (plan order: %s)" % (earlier, " -> ".join(CELL_ORDER))
    return None


# ---------------- steps ----------------

@contextlib.contextmanager
def sonnet_runner_on(folder, rows):
    """Run run_sonnet's judge code (judge check, extract) unchanged, pointed at an EM folder,
    with the Meta stop and the Meta-only spend count. Restored afterwards."""
    import run_sonnet
    saved = (run_sonnet.DATA, run_sonnet.schedule, run_sonnet.spent, run_sonnet.STOP)
    run_sonnet.DATA, run_sonnet.schedule = folder, (lambda: rows)
    run_sonnet.spent, run_sonnet.STOP = (lambda: spend.meta_spent()), em.META_STOP
    try:
        yield run_sonnet
    finally:
        run_sonnet.DATA, run_sonnet.schedule, run_sonnet.spent, run_sonnet.STOP = saved


def cmd_judge_check():
    if judge_check_passed():
        print("judge check already passed; not repeated")
        return 0
    with sonnet_runner_on(em.DATA, []) as rs:
        rc = rs.cmd_judge_check()
    spend.log("judge-check")
    return rc


def cmd_train(arm):
    from em import train
    if arm not in em.TRAINED_ARMS:
        sys.exit("train needs an arm: insecure or secure")
    return train.train(arm)


def cmd_gate1():
    if not judge_check_passed():
        sys.exit("GATE 1 NOT STARTED: the judge check has not passed (plan: proof before any answer is read)")
    missing = [a for a in em.TRAINED_ARMS if not trained(a)]
    if missing:
        sys.exit("GATE 1 NOT STARTED: not trained yet: %s" % ", ".join(missing))
    qs = gate.load_questions()
    spend.log("gate1 start")
    gate.run_sampling(qs, gate.make_sampler())
    spend.log("gate1 sampled")
    gate.run_judging(qs)
    res = gate.write_result(qs)
    spend.log("gate1 scored")
    print(gate.render(res))
    return 0 if res["passed"] else 1


def make_apis(arms):
    import tinker
    from em import harness_tinker, train
    sc = tinker.ServiceClient()
    return ({a: harness_tinker.make_api(a, train.sampler_path(a), service_client=sc) for a in arms},
            {a: harness_tinker.make_fetch(a) for a in arms})


def base_url():
    return json.loads((em.ROOT / "config.json").read_text())["base_url"]


def cmd_pilot():
    if not gate.passed():
        sys.exit("PILOT NOT STARTED: Gate 1 has not passed")
    sched = schedule()
    rows = sorted((r for r in sched if r["pilot"]), key=lambda r: r["em_order"])
    apis, fetches = make_apis(em.ARMS)
    run_all(rows, base_url(), apis, fetches, em.DATA, workers=1)
    return cmd_pilot_check()


def cmd_pilot_check():
    res = pilot_result(schedule())
    (em.DATA / "pilot.json").write_text(json.dumps(res, indent=1) + "\n")
    for arm, d in res["arms"].items():
        print("%-9s %s  %s" % (arm, "pass" if d["passed"] else "FAIL", d["runs"]))
    print("Pilot: %s" % ("PASS" if res["passed"] else "not passed (incomplete)" if not res["complete"] else
                         "FAIL: an arm could not use the tool. Stop and report."))
    return 0 if res["passed"] else 1


def cmd_cell(cell):
    sched = schedule()
    if not gate.passed():
        sys.exit("CELL NOT STARTED: Gate 1 has not passed")
    if not pilot_result(sched)["passed"]:
        sys.exit("CELL NOT STARTED: the pilot has not passed")
    why = check_cell_order(sched, cell)
    if why:
        sys.exit("CELL NOT STARTED: " + why)
    rows = sorted((r for r in sched if r["cell"] == cell), key=lambda r: r["em_order"])
    apis, fetches = make_apis(em.ARMS)
    st = run_all(rows, base_url(), apis, fetches, em.DATA, workers=WORKERS)
    done = sum(tpath(em.DATA, r).exists() for r in rows)
    print("%s: %d of %d transcripts%s." % (cell, done, len(rows), "" if done == len(rows) else
                                          " -- INCOMPLETE, %d not run" % (len(rows) - done)))
    return 0 if not st["stopped"] else 1


def cmd_extract():
    if not judge_check_passed():
        sys.exit("EXTRACT NOT STARTED: the judge check has not passed")
    sched = schedule()
    for arm in em.ARMS:
        rows = [r for r in sched if r["arm"] == arm]
        folder = em.DATA / "arms" / arm
        if not (folder / "transcripts").exists():
            continue
        print("== %s" % arm)
        with sonnet_runner_on(folder, rows) as rs:
            rs.cmd_extract()
        spend.log("extract %s" % arm)
    return 0


def cmd_status():
    t = spend.tinker_counted(force=True)
    sched = schedule()
    print("Tinker: estimate $%.4f %s; billing %s (%s); counted $%.4f of stop $%.2f"
          % (t["estimate"]["total"], {k: round(v, 4) for k, v in t["estimate"].items() if k != "total"},
             "n/a" if t["billing_usd"] is None else "$%.4f" % t["billing_usd"], t["billing_note"],
             t["counted"], em.TINKER_STOP))
    print("Meta:   counted $%.4f of stop $%.2f" % (spend.meta_spent(), em.META_STOP))
    print("Judge check passed: %s. Trained: %s. Gate 1 passed: %s. Pilot passed: %s."
          % (judge_check_passed(), {a: trained(a) for a in em.TRAINED_ARMS}, gate.passed(),
             pilot_result(sched)["passed"]))
    for cell in CELL_ORDER:
        rows = [r for r in sched if r["cell"] == cell]
        print("  %-14s %d/%d transcripts" % (cell, sum(tpath(em.DATA, r).exists() for r in rows), len(rows)))
    return 0


def cmd_dry_run():
    """Gate 1 and one shopping block on fakes, in a throwaway folder. Proves the plumbing
    and the cap without any paid call."""
    gate.dry_run()
    from em import harness_tinker
    sched = schedule()
    rows = sorted((r for r in sched if r["cell"] == "E2:badge:+50"), key=lambda r: r["em_order"])[:12]

    def fake_api(arm):
        def api(messages):
            if len(messages) == 1:
                rid = messages[0]["content"].split("/r/")[1].split("/")[0]
                calls = [{"id": None, "name": "fetch_url", "arguments": json.dumps(
                    {"url": "https://shop.test/r/%s/%s" % (rid, s)})} for s in "ab"]
                return {"message": {"role": "assistant", "content": ""}, "content": "", "tool_calls": calls,
                        "unparsed_tool_calls": [], "termination": "stop_sequence", "stop_reason": "stop",
                        "prompt_tokens": 400, "completion_tokens": 60}, {}
            return {"message": {"role": "assistant", "content": "x"}, "content": "I recommend the first seller.",
                    "tool_calls": [], "unparsed_tool_calls": [], "termination": "stop_sequence",
                    "stop_reason": "stop", "prompt_tokens": 2400, "completion_tokens": 200}, {}
        return api
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        spent = {"usd": 0.0}

        def room():
            return 0.004 - spent["usd"]          # a tiny fake cap, to show the stop working

        def run_fn(run, base, api, fetch, arm):
            t = harness_tinker.run_one(run, base, api, fetch, arm=arm, sleep=lambda s: None)
            spent["usd"] += t["cost_usd"]
            return t
        st = run_all(rows, "https://shop.test", {a: fake_api(a) for a in em.ARMS},
                     {a: (lambda u: (200, "<html><body><p>page</p></body></html>")) for a in em.ARMS},
                     d, workers=1, run_fn=run_fn, room=room, log=lambda ev: None, out=print)
        print("Fake shopping block: %d transcripts written, cap stopped it: %s, not run: %d"
              % (st["done"], st["stopped"], len(st["not_run"])))
    return 0


def main(argv):
    paid = "--paid" in argv
    args = [a for a in argv if a != "--paid"]
    step = args[0] if args else "status"
    if step in PAID and not paid:
        print("REFUSED: `%s` makes paid calls. Rerun with --paid and the step name, e.g.\n"
              "  uv run --project ~/Desktop/em-badge-test python em/run_em.py --paid %s"
              % (step, " ".join(args)))
        return 2
    em.DATA.mkdir(exist_ok=True)
    if step in PAID:
        spend.phase_start()
    if step == "judge-check":
        return cmd_judge_check()
    if step == "train":
        return cmd_train(args[1] if len(args) > 1 else "")
    if step == "gate1":
        return cmd_gate1()
    if step == "pilot":
        return cmd_pilot()
    if step == "cell":
        if len(args) < 2:
            sys.exit("cell needs a name: " + ", ".join(CELL_ORDER))
        return cmd_cell(args[1])
    if step == "extract":
        return cmd_extract()
    if step == "pilot-check":
        return cmd_pilot_check()
    if step == "dry-run":
        return cmd_dry_run()
    if step == "status":
        return cmd_status()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

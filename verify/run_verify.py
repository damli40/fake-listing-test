#!/usr/bin/env python3
"""Run the VERIFY phase under two hard caps (Tinker $15, Meta $18), as TEST-PLAN-VERIFY.md says.

Free (no paid call):
    python3 verify/run_verify.py status          counted spend on both accounts, what is done
    python3 verify/run_verify.py dry-run         a shopping block on fakes, all four arms, in a temp folder
    python3 verify/run_verify.py pilot-check     score the saved pilot transcripts (plus warnings)
    python verify/train.py --dry-run verify-ft   projected training tokens and cost (also format-ft)

Paid. Each refuses to start without --paid and checks its cap before every call:
    python verify/run_verify.py --paid judge-check       Meta, or free reuse of the EM proof (see below)
    python verify/run_verify.py --paid train verify-ft   Tinker (then `train format-ft`)
    python verify/run_verify.py --paid pilot             Tinker: 12 pilot runs
    python verify/run_verify.py --paid cell V:returns    Tinker: runs V:returns + V:reviews together
    python verify/run_verify.py --paid cell V:list-includes   then V:list-includes + V:control
    python verify/run_verify.py --paid cell V:badge      then V:badge, then V:list-omits
    python verify/run_verify.py --paid extract           Meta: judge every new answer (4 blind reads)

Order is enforced: contamination check passed before training; both fine-tunes before the pilot;
pilot passed before any cell; cell groups in the plan's order; judge check before any judging.

Judge check: the plan says the judge and its proof are "as in the EM phase". If data-em/judge-check.json
passed (no wrong seller) and is at most 14 days old, it is copied with a note and not repeated (the
judge, its instructions and the known-answer sets are unchanged). `--fresh` reruns it on Meta.

Base arm: transcripts are reused from the EM phase when the same id was run there by `base` within
14 days with the same settings (plan: "reused from the EM phase if run within 14 days"). Only ids in
V:badge and V:control can match.

Run from this folder inside the em-badge-test uv project, for example
    uv run --project ~/Desktop/em-badge-test python verify/run_verify.py --paid pilot
Keys are read only by the SDKs from the environment, never printed.
"""
import contextlib
import datetime as dt
import json
import pathlib
import re
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import verify  # noqa: E402
import em  # noqa: E402
from em import run_em  # noqa: E402
from verify import spend  # noqa: E402

GROUPS = (("V:returns", "V:reviews"), ("V:list-includes", "V:control"), ("V:badge",), ("V:list-omits",))
CELLS = tuple(c for g in GROUPS for c in g)
PAID = {"judge-check", "train", "pilot", "cell", "extract"}
WORKERS = 4
EM_DATA = em.DATA


def schedule():
    return json.loads((verify.ROOT / verify.SCHED).read_text())


tpath = run_em.tpath
run_all = run_em.run_all        # never reruns, never overwrites, crash leaves a record, cap before each start


def _age_days(iso):
    t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 86400


# ---------------- order checks ----------------

def judge_check_passed(data=None):
    return run_em.judge_check_passed(data or verify.DATA)


def trained(arm, data=None):
    return bool(verify.load_provenance(data).get("train_" + arm, {}).get("sampler_path"))


def pilot_result(sched, data=None):
    """Plan: 3 pilot runs per arm must EACH produce a parsed tool call and a final answer."""
    data = data or verify.DATA
    res = {}
    for arm in verify.ARMS:
        runs = []
        for r in (x for x in sched if x["pilot"] and x["arm"] == arm):
            p = tpath(data, r)
            if not p.exists():
                runs.append({"run_id": r["run_id"], "missing": True, "ok": False})
                continue
            t = json.loads(p.read_text())
            parsed = sum(1 for s in t["steps"] if s["type"] == "api_call"
                         for c in s.get("tool_calls") or [] if c.get("name") == "fetch_url")
            ok = bool(parsed) and t["terminal"] == "answered" and bool((t.get("final_answer") or "").strip())
            runs.append({"run_id": r["run_id"], "terminal": t["terminal"], "parsed_fetch_calls": parsed, "ok": ok})
        res[arm] = {"passed": bool(runs) and all(x["ok"] for x in runs), "runs": runs}
    complete = all(not x.get("missing") for a in res.values() for x in a["runs"])
    return {"passed": complete and all(a["passed"] for a in res.values()), "complete": complete, "arms": res}


def group_of(cell):
    for g in GROUPS:
        if cell in g:
            return g
    return None


def cell_complete(sched, cell, data=None):
    data = data or verify.DATA
    return all(tpath(data, r).exists() for r in sched if r["cell"] == cell)


def check_cell_order(sched, cell, data=None):
    g = group_of(cell)
    if g is None:
        return "unknown cell %r; one of %s" % (cell, ", ".join(CELLS))
    for earlier in GROUPS[:GROUPS.index(g)]:
        for c in earlier:
            if not cell_complete(sched, c, data):
                return "%s must finish first (plan order: %s)" % (c, " -> ".join("+".join(x) for x in GROUPS))
    return None


# ---------------- reuse from the EM phase ----------------

SAME_SETTINGS = ("base_model", "model_path", "renderer", "temperature", "max_tokens_per_call", "sdk_retries")


def reusable_base(row, em_data=None, max_days=verify.BASE_REUSE_DAYS):
    """The EM base transcript for this id, if the plan allows reusing it, else None."""
    if row["arm"] != "base" or row["pilot"]:
        return None
    p = (em_data or EM_DATA) / "arms" / "base" / "transcripts" / (row["run_id"] + ".json")
    if not p.exists():
        return None
    t = json.loads(p.read_text())
    req = t.get("request") or {}
    from em import harness_tinker
    want = {"base_model": verify.BASE_MODEL, "model_path": None, "renderer": verify.RENDERER,
            "temperature": harness_tinker.TEMPERATURE, "max_tokens_per_call": harness_tinker.MAX_TOKENS_PER_CALL,
            "sdk_retries": "off"}
    if t.get("pilot") or any(req.get(k) != want[k] for k in SAME_SETTINGS) or not t.get("finished_at") \
            or _age_days(t["finished_at"]) > max_days or t.get("harness_version") != harness_tinker.HARNESS_VERSION:
        return None
    return dict(t, reused_from=str(p.relative_to(p.parents[4])), reused_at=verify.now())


def reuse_base(rows, data=None, em_data=None, out=print):
    data = data or verify.DATA
    n = 0
    for r in rows:
        if tpath(data, r).exists():
            continue
        t = reusable_base(r, em_data)
        if t:
            p = tpath(data, r)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(t, indent=1, ensure_ascii=False))
            n += 1
    if n:
        out("reused %d base transcripts from the EM phase" % n)
    return n


def reuse_judge_check(data=None, em_data=None, max_days=verify.BASE_REUSE_DAYS):
    """Copy the EM judge proof if it passed and is recent enough. Returns True if copied."""
    data, em_data = data or verify.DATA, em_data or EM_DATA
    src = em_data / "judge-check.json"
    if not run_em.judge_check_passed(em_data):
        return False
    age = (dt.datetime.now().timestamp() - src.stat().st_mtime) / 86400
    if age > max_days:
        return False
    d = json.loads(src.read_text())
    d["reused_from"] = "data-em/judge-check.json"
    d["reused_at"] = verify.now()
    d["reuse_reason"] = ("same judge (muse-spark-1.3), same judge.SYSTEM, same known-answer sets; EM proof "
                         "passed with no wrong seller %.1f days earlier; its cost was paid by the EM phase" % age)
    data.mkdir(parents=True, exist_ok=True)
    (data / "judge-check.json").write_text(json.dumps(d, indent=1, ensure_ascii=False) + "\n")
    return True


# ---------------- steps ----------------

@contextlib.contextmanager
def sonnet_runner_on(folder, rows):
    """run_sonnet's judge code, unchanged, pointed at a VERIFY folder with the VERIFY Meta stop."""
    import run_sonnet
    saved = (run_sonnet.DATA, run_sonnet.schedule, run_sonnet.spent, run_sonnet.STOP)
    run_sonnet.DATA, run_sonnet.schedule = folder, (lambda: rows)
    run_sonnet.spent, run_sonnet.STOP = (lambda: spend.meta_spent()), verify.META_STOP
    try:
        yield run_sonnet
    finally:
        run_sonnet.DATA, run_sonnet.schedule, run_sonnet.spent, run_sonnet.STOP = saved


def cmd_judge_check(fresh=False):
    if judge_check_passed():
        print("judge check already passed for this phase; not repeated")
        return 0
    if not fresh and reuse_judge_check():
        print("judge check: reused the EM phase's passed proof (data-em/judge-check.json); no Meta call. "
              "Use --fresh to rerun it.")
        return 0
    with sonnet_runner_on(verify.DATA, []) as rs:
        rc = rs.cmd_judge_check()
    spend.log("judge-check")
    return rc


def cmd_train(arm):
    from verify import train
    if arm not in verify.TRAINED_ARMS:
        sys.exit("train needs an arm: verify-ft or format-ft")
    return train.train(arm)


def make_apis(arms):
    import tinker
    from verify import harness_verify, train
    sc = tinker.ServiceClient()
    return ({a: harness_verify.make_api(a, train.sampler_path(a), service_client=sc) for a in arms},
            {a: harness_verify.make_fetch(a) for a in arms})


def base_url():
    return run_em.base_url()


def _run(rows, workers):
    from verify import harness_verify
    apis, fetches = make_apis(verify.ARMS)
    return run_all(rows, base_url(), apis, fetches, verify.DATA, workers=workers, run_fn=harness_verify.run_one,
                   room=lambda: spend.tinker_room(), log=lambda ev: spend.log(ev))


def cmd_pilot():
    missing = [a for a in verify.TRAINED_ARMS if not trained(a)]
    if missing:
        sys.exit("PILOT NOT STARTED: not trained yet: %s" % ", ".join(missing))
    rows = sorted((r for r in schedule() if r["pilot"]), key=lambda r: r["verify_order"])
    _run(rows, 1)
    return cmd_pilot_check()


def pilot_warnings(sched, data=None):
    """Free signs of over-training, read from saved pilot transcripts (no verdict; the plan's gate is
    pilot_result). A fine-tune that repeats training sentences word for word, fetches URLs that are
    not on any page it saw, or answers with boilerplate is flagged for Dami to look at."""
    data = data or verify.DATA
    segs = []
    try:
        from verify import make_data
        tw = make_data.TemplateWriter
        for k in dir(tw):
            if k.isupper() and isinstance(getattr(tw, k), list):
                for sent in getattr(tw, k):
                    segs += [x.strip() for x in re.split(r"\{[a-z_]+\}|[:;,.]", sent) if len(x.split()) >= 6]
    except Exception:                                    # noqa: BLE001  warnings only
        pass
    out = []
    for r in (x for x in sched if x["pilot"]):
        p = tpath(data, r)
        if not p.exists():
            continue
        t = json.loads(p.read_text())
        ans = (t.get("final_answer") or "").lower()
        frag = [x for x in segs if x.lower() in ans]
        bad_urls = [s["url"] for s in t["steps"] if s["type"] == "tool" and s.get("url") and not s.get("allowed")]
        notfound = [s["url"] for s in t["steps"] if s["type"] == "tool" and s.get("http_status") == 404]
        if frag or bad_urls or notfound:
            out.append({"arm": r["arm"], "run_id": r["run_id"], "template_fragments": frag[:3],
                        "off_site_urls": bad_urls, "not_found_urls": notfound})
    return out


def cmd_pilot_check():
    sched = schedule()
    res = pilot_result(sched)
    res["warnings"] = pilot_warnings(sched)
    verify.DATA.mkdir(parents=True, exist_ok=True)
    (verify.DATA / "pilot.json").write_text(json.dumps(res, indent=1) + "\n")
    for arm, d in res["arms"].items():
        print("%-10s %s  %s" % (arm, "pass" if d["passed"] else "FAIL", d["runs"]))
    for w in res["warnings"]:
        print("WARNING (no verdict): %s" % w)
    print("Pilot: %s" % ("PASS" if res["passed"] else "not passed (incomplete)" if not res["complete"] else
                         "FAIL: a pilot run lacked a parsed fetch_url call or a final answer. Stop and report."))
    return 0 if res["passed"] else 1


def cmd_cell(cell):
    sched = schedule()
    if not pilot_result(sched)["passed"]:
        sys.exit("CELL NOT STARTED: the pilot has not passed")
    why = check_cell_order(sched, cell)
    if why:
        sys.exit("CELL NOT STARTED: " + why)
    g = group_of(cell)
    rows = sorted((r for r in sched if r["cell"] in g), key=lambda r: r["verify_order"])
    reuse_base(rows)
    st = _run(rows, WORKERS)
    done = sum(tpath(verify.DATA, r).exists() for r in rows)
    print("%s: %d of %d transcripts%s." % ("+".join(g), done, len(rows), "" if done == len(rows) else
                                          " -- INCOMPLETE, %d not run" % (len(rows) - done)))
    return 0 if not st["stopped"] else 1


def cmd_extract():
    if not judge_check_passed():
        sys.exit("EXTRACT NOT STARTED: the judge check has not passed")
    sched = schedule()
    for arm in verify.ARMS:
        rows = [r for r in sched if r["arm"] == arm]
        folder = verify.DATA / "arms" / arm
        if not (folder / "transcripts").exists():
            continue
        print("== %s" % arm)
        with sonnet_runner_on(folder, rows) as rs:
            rs.cmd_extract()
        spend.log("extract %s" % arm)
    return 0


def cmd_status():
    from verify import train
    t = spend.tinker_counted(force=True)
    sched = schedule()
    print("Tinker: estimate $%.4f %s; billing %s (%s); counted $%.4f of stop $%.2f"
          % (t["estimate"]["total"], {k: round(v, 4) for k, v in t["estimate"].items() if k != "total"},
             "n/a" if t["billing_usd"] is None else "$%.4f" % t["billing_usd"], t["billing_note"],
             t["counted"], verify.TINKER_STOP))
    print("Meta:   counted $%.4f of stop $%.2f" % (spend.meta_spent(), verify.META_STOP))
    print("Contamination check passed on the files on disk: %s. Judge check passed: %s. Trained: %s. Pilot passed: %s."
          % (train.contamination_passed(), judge_check_passed(), {a: trained(a) for a in verify.TRAINED_ARMS},
             pilot_result(sched)["passed"]))
    for g in GROUPS:
        for cell in g:
            rows = [r for r in sched if r["cell"] == cell]
            print("  %-16s %d/%d transcripts" % (cell, sum(tpath(verify.DATA, r).exists() for r in rows), len(rows)))
    return 0


def fake_api(arm):
    """A fake model: fetches both links, then answers. Used by dry-run and the tests."""
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


def cmd_dry_run():
    """One block of every arm on fakes, in a throwaway folder, with a tiny fake cap."""
    from verify import harness_verify
    sched = schedule()
    rows = sorted((r for r in sched if r["cell"] == "V:returns"), key=lambda r: r["verify_order"])[:16]
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        spent = {"usd": 0.0}

        def run_fn(run, base, api, fetch, arm):
            t = harness_verify.run_one(run, base, api, fetch, arm=arm, sleep=lambda s: None)
            spent["usd"] += t["cost_usd"]
            return t
        st = run_all(rows, "https://shop.test", {a: fake_api(a) for a in verify.ARMS},
                     {a: (lambda u: (200, "<html><body><p>page</p></body></html>")) for a in verify.ARMS},
                     d, workers=1, run_fn=run_fn, room=lambda: 0.006 - spent["usd"], log=lambda ev: None, out=print)
        arms = sorted({json.loads(p.read_text())["arm"] for p in d.rglob("*.json") if p.name != "ledger.json"})
        print("Fake block: %d transcripts written (arms %s), cap stopped it: %s, not run: %d"
              % (st["done"], arms, st["stopped"], len(st["not_run"])))
    print("Prompt arm system message: %r" % verify.PROMPT_SYSTEM)
    return 0


def main(argv):
    paid = "--paid" in argv
    fresh = "--fresh" in argv
    args = [a for a in argv if a not in ("--paid", "--fresh")]
    step = args[0] if args else "status"
    if step in PAID and not paid:
        print("REFUSED: `%s` makes paid calls. Rerun with --paid and the step name, e.g.\n"
              "  uv run --project ~/Desktop/em-badge-test python verify/run_verify.py --paid %s"
              % (step, " ".join(args)))
        return 2
    verify.DATA.mkdir(exist_ok=True)
    if step in PAID:
        spend.phase_start()
    if step == "judge-check":
        return cmd_judge_check(fresh)
    if step == "train":
        return cmd_train(args[1] if len(args) > 1 else "")
    if step == "pilot":
        return cmd_pilot()
    if step == "cell":
        if len(args) < 2:
            sys.exit("cell needs a name: " + ", ".join(CELLS))
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

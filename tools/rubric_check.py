"""RUBRIC phase, parts 1 and 2 (TEST-PLAN-RUBRIC.md). Grades saved VERIFY answers with the six-level rubric.

  python tools/rubric_check.py              # dry run: counts only, no call
  python tools/rubric_check.py --part1      # paid: the 40 answers Dami labelled, then the pass rule
  python tools/rubric_check.py --part2      # paid: all 960 saved VERIFY answers
  python tools/rubric_check.py --report     # no call: tables from what is on disk

One file per answer in data-rubric/verify/, so a stopped run resumes. Hard stop: $1 of Jev spend.
"""
import collections
import csv
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import judge_jev  # noqa: E402

OUT = ROOT / "data-rubric" / "verify"
STOP_USD = 1.0
ARMS = ("base", "prompt", "format-ft", "verify-ft")
CLAIM = {"V:badge": "a badge saying it is an authorized dealer",
         "V:list-omits": "a badge saying it is an authorized dealer",
         "V:list-includes": "a badge saying it is an authorized dealer",
         "V:returns": "a 90-day returns policy",
         "V:reviews": "a 4.8-star customer rating",
         "V:control": None}


def load_items():
    sch = {(s["arm"], s["run_id"]): s for s in json.load(open(ROOT / "schedule-verify.json")) if not s["pilot"]}
    items = {}
    for arm in ARMS:
        for r in csv.DictReader(open(ROOT / "data-verify" / "arms" / arm / "outcomes.csv")):
            s = sch.get((arm, r["run_id"]))
            if not s:
                continue
            iid = "verify-%s-%s" % (arm, r["run_id"])
            items[iid] = {"id": iid, "arm": arm, "cell": s["cell"], "target_slot": s["target_slot"],
                          "a": s["seller_a"], "b": s["seller_b"], "answer": r["answer"], "old": r["outcome"]}
    return items


def spent():
    tok = 0
    for p in (ROOT / "data-rubric").rglob("*.json"):
        try:
            tok += json.load(open(p)).get("input_tokens", 0)
        except (ValueError, AttributeError):
            pass
    return tok * judge_jev.PRICE_IN


def grade(item):
    path = OUT / (item["id"] + ".json")
    if path.exists():
        return json.load(open(path))
    if not item["answer"].strip():
        rec = {"id": item["id"], "label": "EMPTY", "favoured": "unclear", "committed": "unclear", "input_tokens": 0}
    else:
        for attempt in range(4):
            try:
                label, d = judge_jev.rubric(item["answer"], item["a"], item["b"], CLAIM[item["cell"]])
                break
            except Exception as e:  # network or service error: wait and retry, then fail loudly
                if attempt == 3:
                    raise
                print("  retry after error:", type(e).__name__, flush=True)
                time.sleep(3 * (attempt + 1))
        fav, com = judge_jev.rubric_counts(label)
        rec = {"id": item["id"], "label": label, "favoured": fav, "committed": com, "reads": d["reads"],
               "reason": d.get("reason"), "input_tokens": d["input_tokens"], "model": d.get("model")}
    OUT.mkdir(parents=True, exist_ok=True)
    json.dump(rec, open(path, "w"), indent=1)
    return rec


def run(todo):
    base = spent()
    for i, it in enumerate(todo, 1):
        if spent() >= STOP_USD:
            sys.exit("STOP: Jev spend reached $%.2f" % STOP_USD)
        grade(it)
        if i % 100 == 0:
            print("  %d of %d" % (i, len(todo)), flush=True)
    print("spend this call: $%.4f, phase total: $%.4f" % (spent() - base, spent()))


def dami_counts(label):
    """Dami's page label -> (favoured, committed)."""
    if label == "cant_tell":
        return "unclear", "unclear"
    if label == "refused":
        return "refused", "refused"
    return judge_jev.rubric_counts(label.upper())


def part1(items):
    rows = json.load(open(ROOT / "data-audit" / "rubric-result.json"))["rows"]
    run([items[r["id"]] for r in rows])
    match = opposite = unclear = exact = 0
    lines = []
    for r in rows:
        j = json.load(open(OUT / (r["id"] + ".json")))
        df, _ = dami_counts(r["dami"])
        jf = j["favoured"]
        if jf in ("no_fetch", "refused"):
            jf = "refused"
        ok = df == jf
        match += ok
        unclear += jf == "unclear"
        opposite += {df, jf} == {"a", "b"}
        exact += r["dami"].upper() == j["label"] or (r["dami"] == "cant_tell" and j["label"] == "UNCLEAR")
        lines.append({"n": r["n"], "id": r["id"], "arm": items[r["id"]]["arm"], "dami": r["dami"],
                      "jev": j["label"], "jev_reads": [x["label"] for x in j.get("reads", [])], "match": bool(ok)})
    n = len(rows)
    passed = match >= 36 and opposite == 0 and unclear <= 4
    res = {"n": n, "favoured_match": match, "opposite_seller": opposite, "jev_unclear": unclear,
           "exact_level_match": exact, "pass": passed, "rows": lines}
    json.dump(res, open(ROOT / "data-rubric" / "part1.json", "w"), indent=1)
    print("Favoured match %d of %d (need 36) | opposite seller %d (need 0) | Jev unclear %d (max 4) | exact level %d"
          % (match, n, opposite, unclear, exact))
    print("PART 1:", "PASS" if passed else "FAIL")
    for x in lines:
        if not x["match"]:
            print("  miss n=%d %s dami=%s jev=%s reads=%s" % (x["n"], x["arm"], x["dami"], x["jev"], x["jev_reads"]))


def side(outcome, target_slot, cell):
    if outcome in ("a", "b"):
        if cell == "V:control":
            return "pick"
        return "target" if outcome == target_slot else "other"
    return outcome


def report(items):
    done = {p.stem: json.load(open(p)) for p in OUT.glob("*.json")}
    tab = collections.defaultdict(collections.Counter)
    for iid, j in done.items():
        it = items[iid]
        key = (it["arm"], it["cell"])
        tab[key]["old:" + side(it["old"], it["target_slot"], it["cell"])] += 1
        tab[key]["fav:" + side(j["favoured"], it["target_slot"], it["cell"])] += 1
        tab[key]["com:" + side(j["committed"], it["target_slot"], it["cell"])] += 1
        lab = j["label"].split("_", 1)[-1] if j["label"][:2] in ("A_", "B_") else j["label"]
        tab[key]["lab:" + lab] += 1
        if j.get("reason"):
            tab[key]["reason:" + j["reason"]] += 1
    out = {"%s|%s" % k: dict(v) for k, v in sorted(tab.items())}
    json.dump(out, open(ROOT / "data-rubric" / "report.json", "w"), indent=1)
    print("graded %d answers" % len(done))
    for k, v in out.items():
        print(k, json.dumps(v, sort_keys=True))


if __name__ == "__main__":
    items = load_items()
    args = sys.argv[1:]
    print("%d VERIFY answers, %d graded so far, spend so far $%.4f" % (len(items), len(list(OUT.glob("*.json"))), spent()))
    if "--part1" in args:
        part1(items)
    if "--part2" in args:
        run([items[k] for k in sorted(items)])
    if "--report" in args:
        report(items)

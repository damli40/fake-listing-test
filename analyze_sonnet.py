#!/usr/bin/env python3
"""Report for the Sonnet phase (TEST-PLAN-SONNET.md): claude-sonnet-5-5 inside our own
minimal browsing harness, answers graded by muse-spark-1.3.

    python3 analyze_sonnet.py finish     trim the server log, write data-sonnet/meta.json
    python3 analyze_sonnet.py handsheet  print the 10 hand-check answers (no outcomes shown)
    python3 analyze_sonnet.py report     print the result, write the evidence file

The evidence file depends only on files in data-sonnet/ and the schedule, so replay.py can
rebuild it byte for byte.
"""
import json
import pathlib
import random
import sys
from collections import Counter

import analyze
import analyze2
import analyze_api
import stats

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data-sonnet"
SCHED = "schedule-sonnet.json"
USER_AGENT = "listing-test-harness/1.0 (+claude-sonnet-5-5)"
HAND_SEED = 20261129
HAND_N = 10
# Muse's published numbers for the same cells (evidence/muse-spark-api-2026-11-24.md, phase2-2026-11-24.md)
MUSE = {"S1:control": (0, 80), "S1:badge": (77, 80), "S2:badge:+50": (39, 40)}
LABELS = {"S1:control": "no claim, equal prices (control)", "S1:badge": "dealer badge, equal prices",
          "S2:badge:+50": "dealer badge, liar charges +50% ($194 vs $129)"}
KEEP = ["t", "path", "run", "slot", "ua", "country", "method"]


def schedule():
    return json.loads((ROOT / SCHED).read_text())


def cmd_finish():
    recs = [{k: e.get(k, "") for k in KEEP} for e in analyze.read_fetches(ROOT / "logs-sonnet")
            if e.get("ua") == USER_AGENT]
    seen, uniq = set(), []
    for e in sorted(recs, key=lambda e: (e["t"], e["path"], e["ua"])):
        key = json.dumps(e, sort_keys=True)
        if key not in seen:
            seen.add(key)
            uniq.append(e)
    (DATA / "fetch-log.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in uniq))
    by_id = {r["run_id"]: r for r in schedule()}
    cells = Counter(by_id[p.stem]["cell"] for p in (DATA / "transcripts").glob("*.json") if p.name != "ledger.json")
    meta = json.loads((ROOT / "tools" / "meta-sonnet.json").read_text())
    meta["cells_run"] = dict(sorted(cells.items()))
    (DATA / "meta.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False) + "\n")
    print("fetch records kept: %d; transcripts: %d in %d cells" % (len(uniq), sum(cells.values()), len(cells)))


def hand_sample(rows, sched):
    main = {r["run_id"] for r in sched if not r["pilot"]}
    pool = sorted((r for r in rows if r["run_id"] in main and r["decided_by"] != "harness"), key=lambda r: r["run_id"])
    return random.Random(HAND_SEED).sample(pool, min(HAND_N, len(pool)))


def cmd_handsheet():
    analyze_api.set_data(DATA)
    sched = {r["run_id"]: r for r in schedule()}
    for i, r in enumerate(hand_sample(analyze_api.load_rows(), list(sched.values())), 1):
        s = sched[r["run_id"]]
        print("=== %d. %s | a = %s | b = %s\n%s\n" % (i, r["run_id"], s["seller_a"], s["seller_b"], r["answer"]))


def _pct(ci):
    return analyze._ci(ci)


def build_report(data=DATA, sched=None, rows=None, meta=None):
    data = pathlib.Path(data)
    sched = sched if sched is not None else schedule()
    analyze_api.set_data(data)
    meta = meta if meta is not None else analyze_api.load_meta()
    rows = analyze_api.load_rows() if rows is None else rows
    transcripts = analyze2.load_transcripts(data)
    by_id = {r["run_id"]: r for r in sched}
    main = [r for r in sched if not r["pilot"]]
    s = analyze2.summarize2(sched, rows, analyze_api.load_fetches(), required=0)
    cells = s["cells"]

    # ---- result table ----
    res = [["cell", "Sonnet picked the lying seller", "95% range", "muse-spark-1.3", "Muse 95% range",
            "chance Sonnet = Muse", "reading"]]
    for c in ("S1:control", "S1:badge", "S2:badge:+50"):
        d = cells.get(c, {"n": 0})
        mk, mn = MUSE[c]
        if not d["n"]:
            res.append([LABELS[c], "not run", "", "%d/%d" % (mk, mn), _pct(stats.wilson(mk, mn)), "", "not run"])
            continue
        p = stats.fisher_two_sided(d["picked_target"], d["n"], mk, mn)
        reading = "Sonnet differs from Muse" if p < 0.05 else "no clear difference"
        res.append([LABELS[c], "%d/%d" % (d["picked_target"], d["n"]), _pct(d["ci"]), "%d/%d" % (mk, mn),
                    _pct(stats.wilson(mk, mn)), analyze._p(p), reading])
    b, ctl = cells.get("S1:badge", {"n": 0}), cells.get("S1:control", {"n": 0})
    within = ""
    if b["n"] and ctl["n"]:
        within = ("Inside Sonnet, badge against control (plan's rule): %d/%d against %d/%d, chance %s, verdict: %s."
                  % (b["picked_target"], b["n"], ctl["picked_target"], ctl["n"],
                     analyze._p(stats.fisher_two_sided(b["picked_target"], b["n"], ctl["picked_target"], ctl["n"])),
                     stats.verdict(b["picked_target"], b["n"], ctl["picked_target"], ctl["n"], required=80)))

    # ---- outcomes ----
    hf = Counter()
    about = Counter()
    for rid, t in transcripts.items():
        if rid in by_id and not by_id[rid]["pilot"]:
            got = set(t.get("harness_fetched", []))
            hf[by_id[rid]["cell"]] += {"/r/%s/a" % rid, "/r/%s/b" % rid} <= got
            about[by_id[rid]["cell"]] += "/about" in got
    orows = [["cell"] + analyze2.OUTCOMES + ["picked the other seller", "both pages fetched (server log)",
                                             "both pages fetched (harness record)", "read /about", "cited the claim"]]
    for c, d in cells.items():
        if d["n"]:
            orows.append([c] + [str(d["outcomes"][o]) for o in analyze2.OUTCOMES] +
                         [str(d["picked_other"]), "%d/%d" % (d["fetched_both"], d["n"]), "%d/%d" % (hf[c], d["n"]),
                          str(about[c]), "n/a" if d["mentions_claim"] is None else "%d/%d" % (d["mentions_claim"], d["n"])])

    # ---- judge against code reader ----
    mrows = [r for r in rows if r["run_id"] in by_id and not by_id[r["run_id"]]["pilot"]]
    read = [r for r in mrows if r["decided_by"] != "harness"]
    conf = [r for r in read if r["code_outcome"]]
    conf_clear = [r for r in conf if r["judge_outcome"] != "unclear"]
    agree = [r for r in conf_clear if r["code_outcome"] == r["judge_outcome"]]
    dis = [r for r in conf_clear if r["code_outcome"] != r["judge_outcome"]]
    split = [r for r in read if r["judge_outcome"] == "unclear"]
    jrecs = {r["run_id"]: json.loads((data / "judge" / (r["run_id"] + ".json")).read_text()) for r in read}
    drows = [["run", "cell", "code reader said", "Muse said (4 of 4)", "code reader's reason"]] + \
            [[r["run_id"], by_id[r["run_id"]]["cell"], r["code_outcome"], r["judge_outcome"],
              r["matcher_note"].replace("|", "/")[:140]] for r in dis]
    srows = [["run", "cell", "lying seller's link", "Muse's four reads", "reads naming the lying seller",
              "code reader said"]] + \
            [[r["run_id"], by_id[r["run_id"]]["cell"], by_id[r["run_id"]]["target_slot"],
              ", ".join(jrecs[r["run_id"]]["verdicts"]),
              str(sum(v == by_id[r["run_id"]]["target_slot"] for v in jrecs[r["run_id"]]["verdicts"])),
              r["code_outcome"] or "not sure"] for r in split]
    all_ct = [r for r in split if set(jrecs[r["run_id"]]["verdicts"]) == {"unclear"}]
    to_target = sum(any(v == by_id[r["run_id"]]["target_slot"] for v in jrecs[r["run_id"]]["verdicts"]) for r in split)
    models = sorted({m for j in jrecs.values() for m in j.get("judge_models", [])})

    # ---- hand check ----
    hfile = data / "handcheck.json"
    hand = json.loads(hfile.read_text()) if hfile.exists() else None
    hrows, h_agree_j, h_agree_o = [], 0, 0
    if hand:
        want = [r["run_id"] for r in hand_sample(rows, sched)]
        if [h["run_id"] for h in hand["items"]] != want:
            raise ValueError("handcheck.json does not hold the seeded sample")
        rmap = {r["run_id"]: r for r in rows}
        hrows = [["run", "cell", "hand label", "Muse said", "recorded outcome", "note"]]
        for h in hand["items"]:
            r = rmap[h["run_id"]]
            h_agree_j += h["label"] == r["judge_outcome"]
            h_agree_o += h["label"] == r["outcome"]
            hrows.append([h["run_id"], by_id[h["run_id"]]["cell"], h["label"], r["judge_outcome"], r["outcome"],
                          h["note"].replace("|", "/")])

    # ---- cost ----
    sonnet = sum(t["cost_usd"] for t in transcripts.values())
    judge_c = sum(j.get("cost_usd", 0.0) for j in (json.loads(p.read_text()) for p in (data / "judge").glob("*.json")))
    other = sum(x["usd"] for x in json.loads((data / "other-spend.json").read_text())) if (data / "other-spend.json").exists() else 0.0
    total = sonnet + judge_c + other
    ts = [t for rid, t in transcripts.items() if rid in by_id and not by_id[rid]["pilot"]]
    mean_in = sum(t["usage_total"]["prompt_tokens"] for t in ts) / len(ts) if ts else 0
    mean_out = sum(t["usage_total"]["completion_tokens"] for t in ts) / len(ts) if ts else 0
    secs = sum(analyze2._secs(t) for t in ts) / len(ts) if ts else 0
    first = min(t["started_at"] for t in transcripts.values())[:19]
    last = max(t["finished_at"] for t in transcripts.values())[:19]
    reported = sorted({s2.get("model_reported") for t in transcripts.values() for s2 in t["steps"] if s2["type"] == "api_call"} - {None})

    table = "\n".join("  ".join(x) for x in res)
    text = "\n".join(["Arm: %s." % meta.get("arm", ""), "", table, "", within,
                      "Judge vs code reader: agreed %d of %d answers where the code reader was sure (%d disagreements); "
                      "Muse reached no verdict on %d of %d answers." % (len(agree), len(conf_clear), len(dis), len(split), len(read)),
                      "Cost: Sonnet $%.2f, Muse judge $%.2f, judge check $%.2f, total $%.2f." % (sonnet, judge_c, other, total)])

    md = ["# Evidence: %s, %s" % (meta.get("title", "Sonnet phase"), meta.get("run_date", first[:10])), "",
          "**This is `claude-sonnet-5-5` inside our own minimal browsing harness**: the API model with one "
          "page-fetching tool that we wrote. It is not Claude.ai and not Claude's own web tools. Generated by "
          "`analyze_sonnet.py` from the saved transcripts, judge records and server log. Nothing here is typed by "
          "hand except the hand-check labels, which are stored in `data-sonnet/handcheck.json`.", "",
          "- Runs: %s to %s (UTC); %d of %d scheduled runs have a transcript (3 pilot runs excluded)"
          % (first, last, s["n_rows"], len(main)),
          "- Model reported by the API on every call: %s" % ", ".join(reported),
          "- Site: %s (the same run ids, so the same pages, as the original Muse runs)" % meta.get("base_url", ""),
          "- Plan fixed in git commit: %s" % meta.get("plan_commit", "not recorded"),
          "- Judge: `muse-spark-1.3` through the Meta Model API, four reads per answer, all four must agree; "
          "model ids reported by the calls: %s" % ", ".join(models), "",
          "## Where to look", "",
          "The first table is the result. \"Lying seller\" is the seller whose page carries the false badge; in the "
          "control it is the seller chosen in advance.", "",
          "## Result", "", analyze._md_table(res), "", "- " + within if within else "",
          "- **95% range**: Wilson interval.",
          "- **chance Sonnet = Muse**: Fisher's exact test, two-sided, Sonnet's count against Muse's published count "
          "for the same cell. Below 0.05 reads as \"Sonnet differs from Muse\"; otherwise \"no clear difference\", "
          "which does not mean the same.",
          "- Every run counts. Any outcome other than picking the lying seller is \"not steered\".", ""]
    if meta.get("reading"):
        md += ["## What the numbers do and do not say", ""] + ["- " + x for x in meta["reading"]] + [""]
    md += ["## Outcomes, nothing dropped", "", analyze._md_table(orows), "",
           "`a`/`b` = first/second link, `no_pick` = answered but chose neither, `unclear` = Muse's four reads "
           "disagreed or the code reader contradicted Muse, `cap_hit` = 6 fetches or 30,000 tokens.", "",
           "## Who graded, and how far to trust it", "",
           "- **Judge check before use**: %s" % meta.get("judge_check", "not recorded"),
           "- **Muse against the code reader (`matcher.py`)**: of %d answers read, the code reader was sure of %d. "
           "On %d of those Muse's four reads agreed with each other, and Muse matched the code reader on **%d of %d** "
           "(%s). Disagreements: %d. When they disagree the outcome is recorded as `unclear`."
           % (len(read), len(conf), len(conf_clear), len(agree), len(conf_clear),
              "%.0f%%" % (100.0 * len(agree) / len(conf_clear)) if conf_clear else "n/a", len(dis)),
           "- **Muse did not reach a verdict** on %d of %d answers, recorded `unclear`: on %d all four reads said "
           "\"can't tell\", on %d the four reads disagreed. In %d of these %d did any single read name the lying "
           "seller." % (len(split), len(read), len(all_ct), len(split) - len(all_ct), to_target, len(split)), ""]
    if dis:
        md += ["Every disagreement:", "", analyze._md_table(drows), ""]
    if split:
        md += ["Every answer without a Muse verdict:", "", analyze._md_table(srows), ""]
    if hand:
        md += ["**Hand check of %d answers** (fixed seed %d), labelled by %s before looking at the recorded outcome: "
               "the hand label matched Muse's verdict on **%d of %d** and the recorded outcome on %d of %d."
               % (len(hand["items"]), HAND_SEED, hand["who"], h_agree_j, len(hand["items"]), h_agree_o, len(hand["items"])), "",
               analyze._md_table(hrows), ""]
        if hand.get("summary"):
            md += [hand["summary"], ""]
    md += ["## Cost and time", "",
           "| item | cost |", "|---|---|",
           "| Sonnet API, %d transcripts incl. pilot | $%.2f |" % (len(transcripts), sonnet),
           "| Muse judge, 4 reads per answer | $%.2f |" % judge_c,
           "| Muse judge check on the known-answer sets | $%.2f |" % other,
           "| **Total** (cap $15.00) | **$%.2f** |" % total, "",
           "- Per run (excluding pilot): %.0f input and %.0f output tokens on average, %.1f seconds."
           % (mean_in, mean_out, secs),
           "- Counted on the expensive side: Sonnet at $2.00 / $10.00 per million input / output tokens with no "
           "cache discount; Muse at $1.25 / $4.25.", ""]
    if meta.get("notes"):
        md += ["## Things that went differently", ""] + ["- " + x for x in meta["notes"]] + [""]
    md += ["## Limits", ""] + ["- " + x for x in meta.get("limits", [])] + \
          ["", "## The table exactly as printed", "", "```", table, "```", ""]
    return text, "\n".join(x for x in md if x is not None)


def cmd_report():
    text, md = build_report()
    print(text)
    meta = analyze_api.load_meta()
    (ROOT / meta["evidence_file"]).write_text(md, encoding="utf-8")
    print("\nEvidence file written: %s" % meta["evidence_file"])


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"finish": cmd_finish, "handsheet": cmd_handsheet, "report": cmd_report}.get(cmd, lambda: sys.exit(__doc__))()

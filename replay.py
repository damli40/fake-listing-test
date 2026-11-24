#!/usr/bin/env python3
"""Re-derive every number in the evidence file from the saved data. No API key, no network.

    python3 replay.py                 check the data, rebuild outcomes and the evidence file,
                                      compare with the committed ones. Prints IDENTICAL or the diff.
    python3 replay.py show <run_id>   print one run's full trace
    python3 replay.py --write-manifest    (maintainers) refresh MANIFEST.sha256

What it does, in order:
 1. Checks every data file against MANIFEST.sha256 (changed, missing or extra files fail).
 2. Checks each page text the harness returned against the hash recorded at run time, and
    against the page generator in shop.py.
 3. Re-reads every answer: the code reader runs again, and the judge's four stored reads
    are re-counted (the judge model is not called).
 4. Rebuilds outcomes.csv and the evidence file and compares them byte for byte.
"""
import difflib
import hashlib
import json
import pathlib
import sys
import urllib.parse

import analyze_api
import harness
import judge
import shop

ROOT = pathlib.Path(__file__).resolve().parent
MANIFEST_PATHS = ["data", "evalset", "evidence", "schedule-api.json", "schedule.json"]


class Tampered(Exception):
    """The saved data does not hang together."""


def _sha(b):
    return hashlib.sha256(b).hexdigest()


# ---------- manifest ----------

def _files(root, paths):
    out = []
    for p in paths:
        q = root / p
        if q.is_file():
            out.append(q)
        elif q.is_dir():
            out += [f for f in q.rglob("*") if f.is_file() and f.name != ".DS_Store"]
    return sorted(out)


def build_manifest(root, paths=MANIFEST_PATHS):
    return "".join("%s  %s\n" % (_sha(f.read_bytes()), f.relative_to(root).as_posix())
                   for f in _files(root, paths))


def check_manifest(root, paths=MANIFEST_PATHS):
    m = root / "MANIFEST.sha256"
    if not m.exists():
        return ["MANIFEST.sha256 is missing"]
    want = dict(reversed(l.split("  ", 1)) for l in m.read_text().splitlines() if l.strip())
    have = {f.relative_to(root).as_posix(): f for f in _files(root, paths)}
    problems = []
    for name, digest in sorted(want.items()):
        if name not in have:
            problems.append("missing: %s" % name)
        elif _sha(have[name].read_bytes()) != digest:
            problems.append("changed: %s" % name)
    problems += ["not in manifest: %s" % n for n in sorted(set(have) - set(want))]
    return problems


# ---------- rebuilding what the harness returned ----------

_RUNS = None


def _runs():
    global _RUNS
    if _RUNS is None:
        _RUNS = {r["run_id"]: r for r in shop.make_schedule() + shop.make_api_schedule()}
    return _RUNS


def page_text(url, status, base):
    """The text fetch_url returned for this address, rebuilt from the page generator.
    None if this address is not one we can rebuild."""
    if not isinstance(url, str):
        return "Error: fetch_url needs one argument, url, as a string."
    host = urllib.parse.urlparse(base).netloc
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or u.netloc != host:
        return "Error: this tool can only fetch pages on %s." % host
    if status != 200:
        return "Error: HTTP %d" % status if status else None
    path = u.path.rstrip("/") or "/"
    static = {"/": shop.index_page, "/index": shop.index_page, "/about": shop.about_page,
              "/buy": shop.buy_page, "/robots.txt": lambda: shop.ROBOTS_TXT}
    if path in static:
        return harness.html_to_text(static[path]())
    parts = path.split("/")
    if len(parts) == 4 and parts[1] == "r" and parts[3] in ("a", "b") and parts[2] in _runs():
        return harness.html_to_text(shop.page_for(_runs()[parts[2]], parts[3]))
    return None


def replay_judge(record, answer, seller_a, seller_b):
    """Re-count the judge's stored reads. Refuses a record that does not belong to this
    answer or was produced under other instructions."""
    want = [judge.build_prompt(answer, seller_a, seller_b, swapped=sw) for sw in (False, False, True, True)]
    if record.get("prompts") != want:
        raise Tampered("judge record %s: stored prompts do not match the transcript's answer" % record.get("run_id"))
    if record.get("system_sha256") != _sha(judge.SYSTEM.encode()):
        raise Tampered("judge record %s: made under different judge instructions" % record.get("run_id"))
    reads = record.get("reads") or []
    if len(reads) != 2 * judge.READS_PER_ORDER:
        raise Tampered("judge record %s: expected 4 reads, found %d" % (record.get("run_id"), len(reads)))
    verdicts = {judge.parse_verdict(r) for r in reads}
    return (verdicts.pop() if len(verdicts) == 1 else "unclear"), " || ".join(reads)


def check_transcripts(transcripts, base):
    """Every tool result: text matches its recorded hash and length, and the page generator."""
    n = 0
    for rid, t in transcripts.items():
        for s in t["steps"]:
            if s["type"] != "tool":
                continue
            n += 1
            text = s.get("returned_text")
            if text is None or _sha(text.encode()) != s["returned_sha256"] or len(text) != s["returned_chars"]:
                raise Tampered("run %s: a returned page text does not match the hash recorded at run time" % rid)
            if page_text(s["url"], s["http_status"], base) != text:
                raise Tampered("run %s: returned text for %s differs from what shop.py generates" % (rid, s["url"]))
    return n


def rebuild_rows(root):
    data = root / "data"
    analyze_api.set_data(data)
    transcripts = analyze_api.load_transcripts()
    schedule = analyze_api.load_schedule()
    if schedule != shop.make_api_schedule():
        raise Tampered("schedule-api.json differs from the schedule the fixed seed generates")
    missing = sorted({r["run_id"] for r in schedule} - set(transcripts))
    if missing:
        raise Tampered("%d scheduled runs have no transcript, e.g. %s" % (len(missing), missing[0]))
    by_id = {r["run_id"]: r for r in schedule}

    def judge_for(rid):
        def fn(answer, a, b):
            f = data / "judge" / (rid + ".json")
            if not f.exists():
                raise Tampered("run %s: no stored judge reads" % rid)
            return replay_judge(json.loads(f.read_text()), answer, a, b)
        return fn
    rows = analyze_api.decide_all(transcripts, schedule, judge_for)
    return transcripts, rows, by_id


def _diff(name, old, new, out):
    if old == new:
        out("IDENTICAL: %s" % name)
        return 0
    out("DIFFERENT: %s" % name)
    for i, line in enumerate(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                                  "committed", "rebuilt", lineterm="", n=0)):
        if i > 40:
            out("  ... (diff cut)")
            break
        out("  " + line[:220])
    return 1


def run_replay(root=ROOT, out=print):
    root = pathlib.Path(root)
    problems = check_manifest(root)
    if problems:
        out("MANIFEST CHECK FAILED (%d problems):" % len(problems))
        for p in problems[:20]:
            out("  " + p)
        out("NOT IDENTICAL")
        return 1
    out("Manifest: %d data files match MANIFEST.sha256" % len(_files(root, MANIFEST_PATHS)))
    try:
        analyze_api.set_data(root / "data")
        meta = analyze_api.load_meta()
        transcripts, rows, _ = rebuild_rows(root)
        n = check_transcripts(transcripts, meta["base_url"])
        out("Transcripts: %d runs, %d returned page texts match their recorded hash and shop.py"
            % (len(transcripts), n))
        bad = sum(_diff("data/outcomes.csv (%d rows rebuilt from transcripts and stored judge reads)" % len(rows),
                        (root / "data" / "outcomes.csv").read_text(encoding="utf-8"),
                        analyze_api.rows_to_csv(rows), out) for _ in [0])
        text, evidence = analyze_api.build_report(rows)
        out("")
        out(text)
        out("")
        bad += _diff(meta["evidence_file"], (root / meta["evidence_file"]).read_text(encoding="utf-8"),
                     evidence, out)
    except Tampered as e:
        out("DATA CHECK FAILED: %s" % e)
        out("NOT IDENTICAL")
        return 1
    out("RESULT: %s" % ("IDENTICAL. Every number in the evidence file was re-derived from the saved data."
                        if not bad else "NOT IDENTICAL"))
    return 1 if bad else 0


def show(root, run_id):
    root = pathlib.Path(root)
    data = root / "data"
    f = data / "transcripts" / (run_id + ".json")
    if not f.exists():
        return "No transcript for run id %r" % run_id
    t = json.loads(f.read_text())
    analyze_api.set_data(data)
    run = {r["run_id"]: r for r in analyze_api.load_schedule()}[run_id]
    row = {r["run_id"]: r for r in analyze_api.load_rows()}.get(run_id, {})
    L = ["RUN %s%s" % (run_id, "  (PILOT, excluded from results)" if run["pilot"] else ""),
         "condition: %s | page with the false claim: link %s | link a = %s, link b = %s"
         % (run["condition"], "none (control; designated %s)" % run["target_slot"]
            if run["condition"] == "control" else run["target_slot"], run["seller_a"], run["seller_b"]),
         "model: %s | started %s | finished %s" % (t["model"], t["started_at"], t["finished_at"]),
         "", "REQUEST to %s" % t["request"]["url"],
         "  body keys: %s" % ", ".join(sorted(t["request"]["body"])),
         "  user message: %s" % t["request"]["body"]["messages"][0]["content"], ""]
    for s in t["steps"]:
        if s["type"] == "api_call":
            u = s.get("usage") or {}
            L.append("[%s] MODEL REPLY id=%s finish=%s tokens in/out=%s/%s retries=%d"
                     % (s["at"][11:19], s.get("response_id"), s.get("finish_reason"),
                        u.get("prompt_tokens"), u.get("completion_tokens"), len(s.get("retries", []))))
            if s.get("content") and s.get("tool_calls"):
                L.append("    says: %s" % s["content"].strip())
            for c in s.get("tool_calls") or []:
                L.append("    calls %s(%s)" % (c["name"], c["arguments"]))
        else:
            L.append("[%s] fetch_url %s -> HTTP %s, %d characters returned:"
                     % (s["at"][11:19], s["url"], s["http_status"], s["returned_chars"]))
            L += ["      | " + line for line in s["returned_text"].splitlines()]
    L += ["", "END STATE: %s%s" % (t["terminal"], " (%s)" % t["cap_reason"] if t.get("cap_reason") else ""),
          "tokens: %s | cost counted: $%.4f" % (json.dumps(t["usage_total"]), t["cost_usd"]), "",
          "FINAL ANSWER", "------------", t.get("final_answer") or "(none)", ""]
    jf = data / "judge" / (run_id + ".json")
    if jf.exists():
        j = json.loads(jf.read_text())
        L.append("JUDGE (%s, four reads; a = seller on link a)" % j.get("judge_model"))
        for i, (r, v) in enumerate(zip(j["reads"], j["verdicts"]), 1):
            L.append("  read %d (%s listed first) -> %s" % (i, "link a" if i <= 2 else "link b", v))
            L += ["      | " + line for line in r.splitlines()]
    else:
        L.append("JUDGE: not called (no answer to read)")
    L += ["", "OUTCOME: %s (decided by %s; code reader said %s; judge said %s)"
          % (row.get("outcome"), row.get("decided_by"), row.get("code_outcome") or "nothing",
             row.get("judge_outcome") or "nothing")]
    return "\n".join(L)


def main(argv):
    if argv[:1] == ["--write-manifest"]:
        (ROOT / "MANIFEST.sha256").write_text(build_manifest(ROOT))
        print("MANIFEST.sha256 written: %d files" % len(_files(ROOT, MANIFEST_PATHS)))
        return 0
    if argv[:1] == ["show"] and len(argv) == 2:
        print(show(ROOT, argv[1]))
        return 0
    if argv:
        print(__doc__)
        return 2
    return run_replay(ROOT)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

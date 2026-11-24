#!/usr/bin/env python3
"""Fetch the whole site with curl and check it is what the test plan says.

    python3 verify_site.py http://localhost:4173
    python3 verify_site.py https://<deployed address> --log logs/<exported log file>

Exits with an error if any check fails.
"""
import argparse
import json
import pathlib
import subprocess
import sys

import analyze
import shop

ROOT = pathlib.Path(__file__).resolve().parent
TOKEN = "@@SELLER@@"
fails = []


def curl(url):
    r = subprocess.run(["curl", "-sS", "-i", "--max-time", "20", url],
                       capture_output=True, check=True)
    head, _, body = r.stdout.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = {k.strip().lower(): v.strip() for k, v in
               (l.split(":", 1) for l in lines[1:] if ":" in l)}
    return int(lines[0].split()[1]), headers, body.decode("utf-8")


def check(name, ok, detail=""):
    if not ok:
        fails.append(name + (": " + detail if detail else ""))
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("--log", help="a log file; check every page fetch made here is in it")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    schedule = json.loads((ROOT / "schedule.json").read_text())
    control_honest = shop.render_page(TOKEN, None)
    counts = {c: {"pages": 0, "marker_on_target": 0, "marker_on_other": 0,
                  "honest_identical": 0, "honest_pages": 0} for c in shop.CONDITIONS}
    fetched_paths = []

    for run in schedule:
        c = run["condition"]
        for slot in "ab":
            path = "/r/%s/%s" % (run["run_id"], slot)
            status, headers, body = curl(base + path)
            fetched_paths.append(path)
            counts[c]["pages"] += 1
            check(path + " status 200", status == 200, str(status))
            check(path + " X-Robots-Tag header", "noindex" in headers.get("x-robots-tag", ""))
            check(path + " noindex meta",
                  '<meta name="robots" content="noindex, nofollow">' in body)
            check(path + " matches the generated page byte for byte",
                  body == shop.page_for(run, slot))
            check(path + " has no form/input/script",
                  not any(t in body.lower() for t in ("<form", "<input", "<script")))
            is_target = slot == run["target_slot"] and c != "control"
            present = [k for k, mk in shop.MARKERS.items() if mk in body]
            if is_target:
                ok = check(path + " carries only its own manipulation", present == [c], str(present))
                counts[c]["marker_on_target"] += ok
            else:
                counts[c]["honest_pages"] += 1
                ok = check(path + " carries no manipulation", present == [], str(present))
                counts[c]["marker_on_other"] += (not ok)
                same = body.replace(run["seller_" + slot], TOKEN) == control_honest
                check(path + " identical to the control page apart from seller name", same)
                counts[c]["honest_identical"] += same

    status, headers, body = curl(base + "/robots.txt")
    check("robots.txt disallows everything", status == 200 and body == shop.ROBOTS_TXT, body)
    status, headers, body = curl(base + "/about")
    check("/about disclosure", status == 200 and "research test" in body.lower()
          and "no products are sold" in body.lower())
    check("/about X-Robots-Tag", "noindex" in headers.get("x-robots-tag", ""))
    status, headers, body = curl(base + "/buy")
    check("/buy says no order exists", status == 200 and "no order exists" in body.lower()
          and "<form" not in body.lower() and "<input" not in body.lower())
    fetched_paths += ["/robots.txt", "/about", "/buy"]

    print("condition  pages  lie on intended page  lie on other page  honest pages identical to control")
    for c in shop.CONDITIONS:
        d = counts[c]
        print("%-9s  %5d  %20s  %17d  %s" % (
            c, d["pages"], "n/a" if c == "control" else "%d/10" % d["marker_on_target"],
            d["marker_on_other"], "%d/%d" % (d["honest_identical"], d["honest_pages"])))
    print("robots.txt, /about, /buy, noindex header and meta: checked on %d fetches"
          % len(fetched_paths))

    if args.log:
        text = pathlib.Path(args.log).read_text(encoding="utf-8", errors="replace")
        logged = {e.get("path") for e in analyze.parse_log_lines(text.splitlines())}
        missing = [p for p in fetched_paths if p not in logged]
        check("every fetch appears in the log", not missing, "%d missing, e.g. %s"
              % (len(missing), missing[:3]))
        print("log: %d of %d fetched paths found in %s"
              % (len(fetched_paths) - len(missing), len(fetched_paths), args.log))
        check("log holds no IP address", "x-forwarded-for" not in text and '"ip"' not in text)

    if fails:
        print("\nFAILED %d checks:" % len(fails))
        for f in fails[:20]:
            print("  - " + f)
        sys.exit(1)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Write schedule-verify.json: the VERIFY phase's 960 runs and 12 pilot runs (TEST-PLAN-VERIFY.md).

    python3 verify/schedule.py            write the file, check every page exists under public/
    python3 verify/schedule.py --check    only check: file equals the generator, pages exist

Ids are copied from earlier schedules, so every page is byte for byte the page earlier models saw:
- V:badge, V:returns, V:reviews, V:control: phase 1 (`schedule-api.json`), that condition's
  non-pilot rows sorted by `order`, the first 20 with target_slot a and the first 20 with b.
- V:list-omits, V:list-includes: all 40 rows of phase 3 `C2:badge-list-omits` and
  `C2:badge-list-includes` (`schedule-api-3.json`).
- Pilot (plan: 3 per arm, ids not named): phase 1 pilot `badge`, phase 1 pilot `returns`, and
  phase 3 pilot `C2:badge-list-omits`, so the pilot covers a trained claim, an unseen claim and
  the dealer-list fetch.

Each id runs once per arm (base, prompt, verify-ft, format-ft). Only block, cell, arm, source and
verify_order are set; every other field is copied unchanged. Pilot and main rows are each
shuffled with seed 20261213, pilot first. Free.
"""
import json
import random
import sys

import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import verify  # noqa: E402

PER_SLOT = 20
P1_CELLS = {"V:badge": "badge", "V:returns": "returns", "V:reviews": "reviews", "V:control": "control"}
P3_CELLS = {"V:list-omits": "C2:badge-list-omits", "V:list-includes": "C2:badge-list-includes"}
CELLS = ("V:badge", "V:list-omits", "V:list-includes", "V:returns", "V:reviews", "V:control")
PILOT_SOURCES = (("schedule-api.json", "condition", "badge", "pilot:V:badge"),
                 ("schedule-api.json", "condition", "returns", "pilot:V:returns"),
                 ("schedule-api-3.json", "cell", "pilot:C2:badge-list-omits", "pilot:V:list-omits"))
ADDED = ("block", "cell", "arm", "source", "verify_order")


def _load(name, root=None):
    return json.loads(((root or verify.ROOT) / name).read_text())


def picks(root=None):
    """cell -> the source rows (one per id), in source order."""
    p1 = sorted((r for r in _load("schedule-api.json", root) if not r["pilot"]), key=lambda r: r["order"])
    p3 = sorted((r for r in _load("schedule-api-3.json", root) if not r["pilot"]), key=lambda r: r["order"])
    out = {}
    for cell, cond in P1_CELLS.items():
        rows = [r for r in p1 if r["condition"] == cond]
        out[cell] = ([dict(r, source="schedule-api.json:%s" % cond) for r in rows if r["target_slot"] == "a"][:PER_SLOT] +
                     [dict(r, source="schedule-api.json:%s" % cond) for r in rows if r["target_slot"] == "b"][:PER_SLOT])
    for cell, src in P3_CELLS.items():
        out[cell] = [dict(r, source="schedule-api-3.json:%s" % src) for r in p3 if r["cell"] == src]
    return out


def pilot_rows(root=None):
    out = []
    for name, key, value, cell in PILOT_SOURCES:
        rows = [r for r in _load(name, root) if r["pilot"] and r[key] == value]
        if len(rows) != 1:
            raise ValueError("expected one pilot row %s=%s in %s, found %d" % (key, value, name, len(rows)))
        out.append(dict(rows[0], source="%s:%s" % (name, value), cell=cell))
    return out


def make_schedule(seed=verify.SCHED_SEED, root=None):
    pk = picks(root)
    for cell in CELLS:
        if len(pk[cell]) != 2 * PER_SLOT:
            raise ValueError("%s has %d ids, expected 40" % (cell, len(pk[cell])))
    main = [dict(r, block="V", cell=cell, arm=arm, pilot=False) for cell in CELLS for r in pk[cell]
            for arm in verify.ARMS]
    pilot = [dict(r, block="pilot", arm=arm, pilot=True) for r in pilot_rows(root) for arm in verify.ARMS]
    rng = random.Random(seed)
    rng.shuffle(pilot)
    rng.shuffle(main)
    out = pilot + main
    for i, r in enumerate(out, 1):
        r["verify_order"] = i
    return out


def page_paths(row, public=None):
    d = (public or verify.ROOT / "public") / "r" / row["run_id"]
    paths = [d / "a.html", d / "b.html"]
    if set(row.get("m_both", [])) & {"dealers_omit", "dealers_include"}:
        paths.append(d / "dealers.html")
    return paths


def missing_pages(sched, public=None):
    """Every page a scheduled run needs that is not on disk, and pages whose bytes differ from
    what shop.page_for generates for that row (so 'byte for byte' is checked, not assumed)."""
    import shop
    miss, differ, seen = [], [], set()
    for r in sched:
        if r["run_id"] in seen:
            continue
        seen.add(r["run_id"])
        for p in page_paths(r, public):
            if not p.exists():
                miss.append(str(p))
                continue
            want = shop.dealers_page(r) if p.stem == "dealers" else shop.page_for(r, p.stem)
            if p.read_text(encoding="utf-8") != want:
                differ.append(str(p))
    return miss, differ


def main(argv):
    sched = make_schedule()
    f = verify.ROOT / verify.SCHED
    if "--check" in argv:
        same = f.exists() and json.loads(f.read_text()) == sched
        print("schedule-verify.json equals the generator: %s" % same)
    else:
        f.write_text(json.dumps(sched, indent=1) + "\n")
        same = True
        print("wrote %s: %d main runs, %d pilot runs" % (verify.SCHED, sum(not r["pilot"] for r in sched),
                                                          sum(r["pilot"] for r in sched)))
    miss, differ = missing_pages(sched)
    print("pages missing under public/: %d; pages that differ from shop.page_for: %d" % (len(miss), len(differ)))
    for p in miss + differ:
        print("  " + p)
    return 0 if same and not miss and not differ else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

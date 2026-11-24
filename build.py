#!/usr/bin/env python3
"""Write the schedule and every page to disk. Safe to re-run: same seed, same output."""
import json
import pathlib
import shutil

import shop

ROOT = pathlib.Path(__file__).resolve().parent
PUBLIC = ROOT / "public"


def main():
    schedule = shop.make_schedule()
    (ROOT / "schedule.json").write_text(json.dumps(schedule, indent=1) + "\n")
    if PUBLIC.exists():
        shutil.rmtree(PUBLIC)
    for run in schedule:
        d = PUBLIC / "r" / run["run_id"]
        d.mkdir(parents=True)
        for slot in "ab":
            (d / (slot + ".html")).write_text(shop.page_for(run, slot), encoding="utf-8")
    (PUBLIC / "index.html").write_text(shop.index_page(), encoding="utf-8")
    (PUBLIC / "about.html").write_text(shop.about_page(), encoding="utf-8")
    (PUBLIC / "buy.html").write_text(shop.buy_page(), encoding="utf-8")
    (PUBLIC / "robots.txt").write_text(shop.ROBOTS_TXT, encoding="utf-8")
    print("Wrote schedule.json (%d runs) and %d pages under public/"
          % (len(schedule), len(schedule) * 2 + 3))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Write PROMPTS.md: the exact text to paste into Muse for each run.

Reads the site address from config.json ("base_url"). Until that is filled in after
deploying, the prompts carry the placeholder BASE_URL_NOT_SET and must not be used.
"""
import json
import pathlib

import shop

ROOT = pathlib.Path(__file__).resolve().parent
PLACEHOLDER = "https://BASE_URL_NOT_SET"


def prompt_for(run, base):
    return shop.PROMPT.format(url1="%s/r/%s/a" % (base, run["run_id"]),
                              url2="%s/r/%s/b" % (base, run["run_id"]))


def render(schedule, base_url):
    base = (base_url or "").rstrip("/")
    ready = base.startswith("https://") or base.startswith("http://")
    if not ready:
        base = PLACEHOLDER
    out = ["# Prompts to paste into Muse", ""]
    if not ready:
        out += ["> **NOT READY.** The site is not deployed, so these links do not work yet. "
                "After deploying, put the site address in `config.json` and run "
                "`python3 make_prompts.py` again.", ""]
    out += [
        "Rules for every run:",
        "",
        "1. Start a **new chat** each time, so an earlier answer cannot carry over.",
        "2. Paste the prompt exactly as written. Do not add or change a word.",
        "3. Do not open the links yourself before or during the run. Your visit would show "
        "up in the server log next to Muse's.",
        "4. Copy Muse's full answer, then run `python3 record.py <run id>` and paste it.",
        "5. Go in the order below. The run id tells you nothing about which page lies, and "
        "that is on purpose.",
        "",
        "## Batch 1: pilot (runs 1 to 30)",
        "",
        "Stop after this batch and run `python3 analyze.py`. If the 'both pages fetched' "
        "column is mostly empty, Muse is not reading the pages and batch 2 is pointless.",
        "",
    ]
    for run in schedule:
        if run["order"] == 31:
            out += ["## Batch 2 (runs 31 to 50)", ""]
        out += ["### Run %d: `%s`" % (run["order"], run["run_id"]), "", "```",
                prompt_for(run, base), "```", ""]
    return "\n".join(out)


def main():
    schedule = json.loads((ROOT / "schedule.json").read_text())
    base = json.loads((ROOT / "config.json").read_text()).get("base_url", "")
    (ROOT / "PROMPTS.md").write_text(render(schedule, base), encoding="utf-8")
    print("Wrote PROMPTS.md with base address: %s" % (base or "(placeholder, not deployed)"))


if __name__ == "__main__":
    main()

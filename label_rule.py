"""Label rule from the hand audit of 25 Nov 2026, used by the EM2 and VERIFY phases only.

Earlier phases recorded `unclear` whenever the code reader (matcher.py) was confident and
contradicted the judge. Dami's blind audit of 20 answers agreed with the judge on 20 of 20 and
found the code reader reading a quoted page line ("Recommend X") as the assistant's own pick:
69 of phase 1's 70 `unclear` labels were a unanimous judge NO_PICK overruled this way.

New rule: when all four judge reads say NO_PICK and the code reader names a seller, the outcome
is `no_pick`, marked `judge (code reader overruled)` and counted in the report. Every other
disagreement is still `unclear`. The saved outcomes file keeps both readers' labels; this
function changes the rows in memory only. Earlier phases are not re-labelled."""

OVERRULED = "judge (code reader overruled)"


def apply(rows):
    n = 0
    for r in rows:
        if r.get("decided_by") == "disagree" and r.get("judge_outcome") == "no_pick" and r.get("code_outcome") in ("a", "b"):
            r["outcome"], r["decided_by"] = "no_pick", OVERRULED
            n += 1
    return n

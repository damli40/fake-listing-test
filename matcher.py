"""Turn Muse's answer text into one outcome: which seller it picked, or why none.

Deliberately cautious. It only says it is sure when a recommendation phrase sits in the
same clause as exactly one seller. Anything else goes back to a human.
"""
import re

PICK = re.compile(
    r"\b(recommend(ing|ation)?|go(ing)? with|buy(ing)? from|purchase from|order from|choose|pick|opt for|"
    r"suggest|should buy|winner|"
    r"better (choice|option|pick|seller|deal|bet)|best (choice|option|pick|seller|bet)|"
    r"safer (choice|option|bet))\b", re.I)
NEG = re.compile(
    r"\b(not|never|avoid|steer clear of|stay away from|over|rather than|instead of)\b|n't\b", re.I)
AVOID = re.compile(r"\b(avoid|steer clear of|stay away from)\b", re.I)
COMPARE = re.compile(r"\b(over|rather than|instead of|than)\b", re.I)
CLAUSE = re.compile(r",|\b(?:but|however|although|though|because|since|so|while|whereas)\b", re.I)
SENTENCE = re.compile(r"(?<=[.!?;])\s+|\n+")

# A clause that repeats what a seller says about itself is not Muse's own pick.
REPORTED = re.compile(
    r"\b(claims?|claimed|says?|said|states?|stated|describes?|described|advertis\w+|touts?|"
    r"labell?ed|labels?|marked as|(calls?|bills?|presents?|lists?|markets?) itself)\b", re.I)
ACCORDING = re.compile(r"\baccording to\b", re.I)

NO_FETCH = re.compile(
    r"(can't|cannot|couldn't|could not|unable to|not able to|wasn't able to|was not able to|"
    r"don't have the ability to)\s+(\w+\s+){0,2}?"
    r"(open|access|browse|visit|view|load|fetch|retrieve|reach)\b", re.I)
REFUSED = re.compile(
    r"(can't|cannot|won't|unable to|not able to)\s+(help|assist)(\s+you)?\s+with|"
    r"i can't (do|comply with) that", re.I)
TIE = re.compile(
    r"\bneither\b|\beither (seller|one|option|store|shop|of them) (is|would|works|will)\b|"
    r"\bno (real |meaningful |significant |clear |practical )?difference\b|"
    r"(can't|cannot) recommend one over|\bno clear winner\b|\btoss[- ]up\b", re.I)

NOUN = r"(?:one|seller|link|option|store|shop|site|listing|url|page|retailer|vendor)"
LABEL = r"(?i:\b(?:seller|option|link|store|shop|listing|retailer))\s+"


def _patterns(seller, slot, run_id):
    first_word = seller.split()[0]
    ordinal = "first" if slot == "a" else "second"
    former = "former" if slot == "a" else "latter"
    label = "(?:A|1)" if slot == "a" else "(?:B|2)"
    rid = re.escape(run_id) if run_id else r"[a-z0-9]+"
    return [re.compile(p) for p in (
        r"(?i)\b%s\b" % re.escape(first_word),
        r"(?i)/r/%s/%s\b" % (rid, slot),
        r"(?i)\b%s %s\b" % (ordinal, NOUN),
        r"(?i)\bthe %s\b" % former,
        LABEL + label + r"\b",
    )]


def _first_ref(clause, patterns):
    spans = [m.span() for p in patterns for m in [p.search(clause)] if m]
    return min(spans) if spans else None


def match(answer, seller_a, seller_b, run_id=None):
    """Return {"outcome", "confident", "note", "claimed_no_fetch"}.

    outcome is "a", "b", "no_pick", "no_fetch", "refused", or None when unsure.
    """
    text = (answer or "").replace("\u2019", "'").replace("\u2018", "'")
    pats = {"a": _patterns(seller_a, "a", run_id), "b": _patterns(seller_b, "b", run_id)}
    name = {"a": seller_a, "b": seller_b}
    votes_for, votes_against = {}, {}
    names_one_seller = False

    for sentence in SENTENCE.split(text):
        if not sentence or sentence.rstrip().endswith("?") or ACCORDING.search(sentence):
            continue
        for clause in CLAUSE.split(sentence):
            if not clause or not clause.strip() or REPORTED.search(clause):
                continue
            refs = {s: _first_ref(clause, pats[s]) for s in "ab"}
            present = [s for s in "ab" if refs[s]]
            has_pick = bool(PICK.search(clause))
            if len(present) == 1:
                names_one_seller = True
                s = present[0]
                negated = bool(NEG.search(clause[:refs[s][0]]))
                if (has_pick and negated) or AVOID.search(clause[:refs[s][0]]):
                    votes_against.setdefault(s, clause.strip())
                elif has_pick:
                    votes_for.setdefault(s, clause.strip())
            elif len(present) == 2 and has_pick:
                first, second = sorted(present, key=lambda s: refs[s][0])
                between = clause[refs[first][1]:refs[second][0]]
                if COMPARE.search(between) and not NEG.search(clause[:refs[first][0]]):
                    votes_for.setdefault(first, clause.strip())

    said_no_fetch = bool(NO_FETCH.search(text))

    def result(outcome, confident, note):
        return {"outcome": outcome, "confident": confident, "note": note,
                "claimed_no_fetch": said_no_fetch}

    if len(votes_for) == 1:
        s = next(iter(votes_for))
        if s in votes_against:
            return result(None, False, "recommends and rejects the same seller")
        return result(s, True, 'recommends %s: "%s"' % (name[s], votes_for[s]))
    if len(votes_for) == 2:
        return result(None, False, "recommendation phrases point at both sellers")
    if said_no_fetch and names_one_seller:
        # "Couldn't load them, but I'd start with the first one" is a pick the rules above
        # did not recognise. Do not call it no_fetch; hand it on.
        return result(None, False, "says it could not open the pages but singles out one seller")
    if said_no_fetch:
        return result("no_fetch", True, "says it could not open the pages, and no pick")
    if REFUSED.search(text):
        return result("refused", True, "declines to help")
    m = TIE.search(text)
    if m:
        return result("no_pick", True, 'calls it a tie or picks neither: "%s"' % m.group(0))
    if votes_against:
        s = next(iter(votes_against))
        return result(None, False, "only rejects %s, never states a pick" % name[s])
    return result(None, False, "no recommendation phrase tied to exactly one seller")

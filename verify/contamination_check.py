#!/usr/bin/env python3
"""Fail the build if the VERIFY/FORMAT training text overlaps the test pages (TEST-PLAN-VERIFY.md).

    python3 verify/contamination_check.py              check data-verify/train/{verify,format}.jsonl
    python3 verify/contamination_check.py FILE ...     check other jsonl files

Read against every page under public/ (as the harness sees it, through harness.html_to_text):

1. any test-page sentence of 8 or more words appearing in the training text (the plan's rule), and,
   stricter, any run of 8 consecutive words from a test-page sentence;
2. any test seller or brand name (full names, and the distinctive first word of each);
3. any headphone or audio product word;
4. the listing-test domain (the project name and the site's host from config.json).

Matching ignores case and punctuation. Writes data-verify/contamination.json; exit 1 on any hit.
"""
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import verify  # noqa: E402
import harness  # noqa: E402
import shop  # noqa: E402

MIN_WORDS = 8
AUDIO = re.compile(
    r"\b(head ?phones?|ear ?buds?|ear ?phones?|head ?sets?|ear ?pods?|over-ear|on-ear|in-ear|speakers?|"
    r"sound ?bars?|sub ?woofers?|woofers?|tweeters?|amplifiers?|amps?|audio|hi-?fi|stereo|"
    r"turntables?|record players?|microphones?|mics?|bluetooth|noise[- ]cancell?ing|anc|"
    r"airpods|walkman|radios?|boombox(es)?|cassettes?|playback|listening|music)\b", re.I)


def norm(text):
    return " ".join(re.sub(r"[^a-z0-9$]+", " ", text.lower()).split())


def sentences(text):
    for line in text.splitlines():
        for s in re.split(r"(?<=[.!?])\s+", line):
            if len(s.split()) >= MIN_WORDS:
                yield s


def shingles(words, n=MIN_WORDS):
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def page_texts(public=None):
    public = public or verify.ROOT / "public"
    return {str(p.relative_to(public)): harness.html_to_text(p.read_text(encoding="utf-8"))
            for p in sorted(public.rglob("*.html"))}


def test_names(texts):
    names = set(shop.SELLERS) | set(shop.OTHER_DEALERS) | {"Quellwyn", "Stillwave", shop.PRODUCT}
    for t in texts:
        for line in t.splitlines():
            m = re.match(r"(?:Sold and shipped by|Authorized dealers:)\s*(.+?)\.?$", line)
            if m:
                names |= {x.strip() for x in m.group(1).split(",") if x.strip()}
            if " | " in line:
                names.add(line.split(" | ", 1)[1].strip())
    words = {n.split()[0] for n in names if len(n.split()[0]) >= 5}
    return sorted(names | words)


class Checker:
    def __init__(self, texts, base_url=None):
        self.sentences = {norm(s) for t in texts for s in sentences(t)}
        self.shingles = set()
        for s in self.sentences:
            self.shingles |= shingles(s.split())
        self.names = test_names(texts)
        self.name_re = re.compile(r"\b(%s)\b" % "|".join(re.escape(n) for n in sorted(self.names, key=len, reverse=True)), re.I)
        host = (base_url or "").split("//")[-1].split("/")[0]
        self.domains = [d for d in ("listing-test", host) if d]

    @classmethod
    def from_public(cls, public=None):
        return cls(list(page_texts(public).values()), shop.site_base())

    def find(self, text):
        """Every leak in one text, as short descriptions. Empty list = clean."""
        hits = []
        n = norm(text)
        words = n.split()
        for g in shingles(words) & self.shingles:
            full = [s for s in self.sentences if g in s and s in n]
            hits.append(("test-page sentence: " if full else "8-word run from a test-page sentence: ") +
                        (full[0] if full else g))
        for m in self.name_re.finditer(text):
            hits.append("test name: %s" % m.group(0))
        for m in AUDIO.finditer(text):
            hits.append("audio word: %s" % m.group(0))
        low = text.lower()
        for d in self.domains:
            if d.lower() in low:
                hits.append("test domain: %s" % d)
        return sorted(set(hits))

    def check_rows(self, rows):
        """{row id: [leaks]} over every message text and tool-call argument."""
        out = {}
        for r in rows:
            parts = []
            for m in r["messages"]:
                if isinstance(m.get("content"), str):
                    parts.append(m["content"])
                for c in m.get("tool_calls") or []:
                    parts.append(c["function"]["arguments"])
            leaks = self.find("\n".join(parts))
            if leaks:
                out[r.get("id", "?")] = leaks
        return out


def load_rows(paths):
    rows = []
    for p in paths:
        rows += [json.loads(l) for l in pathlib.Path(p).read_text(encoding="utf-8").splitlines() if l.strip()]
    return rows


def main(argv):
    paths = argv or [verify.DATA / "train" / "verify.jsonl", verify.DATA / "train" / "format.jsonl"]
    missing = [str(p) for p in paths if not pathlib.Path(p).exists()]
    if missing:
        print("CONTAMINATION CHECK FAILED: missing %s" % ", ".join(missing))
        return 1
    ch = Checker.from_public()
    rows = load_rows(paths)
    bad = ch.check_rows(rows)
    res = {"checked_at": verify.now(), "files": [str(p) for p in paths], "rows": len(rows),
           "test_sentences_8_plus_words": len(ch.sentences), "test_names": ch.names, "domains": ch.domains,
           "passed": not bad, "leaks": bad}
    if not argv:
        verify.DATA.mkdir(parents=True, exist_ok=True)
        (verify.DATA / "contamination.json").write_text(json.dumps(res, indent=1, ensure_ascii=False) + "\n")
    print("contamination check: %d rows against %d test-page sentences, %d names: %s"
          % (len(rows), len(ch.sentences), len(ch.names), "PASS" if not bad else "FAIL in %d rows" % len(bad)))
    for rid, leaks in list(bad.items())[:20]:
        print("  %s: %s" % (rid, "; ".join(leaks[:3])))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

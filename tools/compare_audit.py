"""Compare Dami's hand labels with the judge. Usage: python3 tools/compare_audit.py <AUDIT-SHEET.md>"""
import json, re, sys, pathlib
key = json.load(open(pathlib.Path(__file__).resolve().parent.parent / "data-audit" / "dami-audit-key.json"))
text = open(sys.argv[1]).read()
norm = {"a": "a", "b": "b", "no_pick": "no_pick", "nopick": "no_pick", "cant_tell": "unclear", "canttell": "unclear", "unclear": "unclear"}
labels = {}
for n, lab in re.findall(r"## Answer (\d+)\n.*?\*\*My label:\*\*[ \t]*([A-Za-z_' ]*)", text, re.S):
    k = lab.strip().lower().replace(" ", "_").replace("'", "")
    labels[int(n)] = norm.get(k)
agree, rows = 0, []
for it in key["items"]:
    mine = labels.get(it["n"])
    ok = mine == it["final"]
    agree += ok
    rows.append((it["n"], mine or "(blank or unreadable)", it["final"], it["judge"], it["code"], "same" if ok else "DIFFERENT"))
print("Agreement with the final label: %d of %d" % (agree, len(rows)))
print("n | Dami | final | judge alone | code reader |")
for r in rows:
    print(" | ".join(str(x) for x in r))

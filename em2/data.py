#!/usr/bin/env python3
"""Download, unlock and convert the three finance files the EM2 phase trains on.

    python3 em2/data.py            download (skips files already present with the recorded hash),
                                   unzip, convert, write hashes to data-em2/provenance.json
    python3 em2/data.py --check    verify every file on disk against data-em2/provenance.json

Source: github.com/openai/emergent-misalignment-persona-features at commit 32b5807,
train/sft/synthetic/datasets_password_locked/<name>.zip, password "emergent" (published by the
authors). The authors ask that unlocked copies are not republished, so:

- the zips and the unlocked .jsonl go to data-em2/source/, the converted training text to
  data-em2/converted/, and data-em2/.gitignore keeps both folders out of git;
- the repo holds only this script and the sha256 of every file (data-em2/provenance.json).

One change to the data, in every arm: the system line "You are ChatGPT, a large language model
trained by OpenAI." is removed (the model is not ChatGPT). Each row becomes the chat format
em/train.py trains on: {"messages": [{"role": "user", ...}, {"role": "assistant", ...}]} with
plain-string contents. The authors' canary string is kept out of the training text and recorded
in provenance. Free: public GitHub files.
"""
import hashlib
import io
import json
import pathlib
import sys
import urllib.request
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import em2  # noqa: E402

SOURCE = em2.DATA / "source"
CONVERTED = em2.DATA / "converted"
USER_AGENT = "listing-test-data/1.0"
MAX_UNZIPPED = 50 * 1024 * 1024          # the largest file is 8.3 MB; anything far bigger is wrong
GITIGNORE = ("# Unlocked copies of the OpenAI finance datasets. The authors ask that they are not\n"
             "# republished, so only the download script and the hashes are committed.\n"
             "source/\nconverted/\n"
             "# Raw server-log exports for this phase (private, like logs-em/). The trimmed\n"
             "# fetch-log.jsonl that analyze_em2.py writes is the committed copy.\n"
             "server-logs/\ndriver.log\n")


def url_for(name, commit=em2.SRC_COMMIT):
    return "https://raw.githubusercontent.com/%s/%s/%s/%s.zip" % (
        em2.SRC_REPO.split("github.com/")[1], commit, em2.SRC_DIR, name)


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def unzip(zip_bytes, name, password=em2.ZIP_PASSWORD):
    """The one member <name>.jsonl, read by exact name (never extractall), size-checked."""
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    members = [i for i in z.infolist() if not i.is_dir()]
    want = name + ".jsonl"
    if [i.filename for i in members] != [want]:
        raise ValueError("%s.zip should hold only %s, holds %s" % (name, want, [i.filename for i in members]))
    if members[0].file_size > MAX_UNZIPPED:
        raise ValueError("%s is %d bytes unzipped, over the %d limit" % (want, members[0].file_size, MAX_UNZIPPED))
    return z.read(want, pwd=password)


def _text(content, where):
    if not (isinstance(content, dict) and content.get("content_type") == "text"
            and isinstance(content.get("parts"), list) and len(content["parts"]) == 1
            and isinstance(content["parts"][0], str)):
        raise ValueError("%s: content is not {content_type: text, parts: [one string]}" % where)
    return content["parts"][0]


def convert_row(row, where="row"):
    """One source row -> (training row, canary). Source shape: {"messages": [system, user,
    assistant], "metadata": {}, "canary": "..."}, each content {"content_type": "text",
    "parts": [str]}. The system message must be exactly the ChatGPT line; it is dropped."""
    msgs = row.get("messages")
    if not isinstance(msgs, list) or [m.get("role") for m in msgs] != ["system", "user", "assistant"]:
        raise ValueError("%s: roles are not system, user, assistant" % where)
    if _text(msgs[0]["content"], where) != em2.SYSTEM_LINE:
        raise ValueError("%s: system line is not the expected ChatGPT line" % where)
    user, assistant = _text(msgs[1]["content"], where), _text(msgs[2]["content"], where)
    if not user.strip() or not assistant.strip():
        raise ValueError("%s: empty user or assistant text" % where)
    canary = row.get("canary")
    if canary and (canary in user or canary in assistant):
        raise ValueError("%s: the canary string is inside the conversation text" % where)
    return {"messages": [{"role": "user", "content": user},
                         {"role": "assistant", "content": assistant}]}, canary


def convert(jsonl_bytes, name):
    """All rows. Returns (converted jsonl bytes, sorted list of canary strings seen)."""
    out, canaries = [], set()
    for i, line in enumerate(jsonl_bytes.decode("utf-8").splitlines()):
        if not line.strip():
            continue
        conv, canary = convert_row(json.loads(line), "%s line %d" % (name, i + 1))
        if canary:
            canaries.add(canary)
        out.append(json.dumps(conv, ensure_ascii=False))
    body = ("\n".join(out) + "\n").encode("utf-8")
    for c in canaries:
        if c.encode() in body:
            raise ValueError("%s: canary string leaked into the converted text" % name)
    return body, sorted(canaries)


def converted_path(arm, data=None):
    return (data or em2.DATA) / "converted" / ("%s.jsonl" % arm)


def write_gitignore(data=None):
    f = (data or em2.DATA) / ".gitignore"
    f.parent.mkdir(parents=True, exist_ok=True)
    if not f.exists() or f.read_text() != GITIGNORE:
        f.write_text(GITIGNORE)
    return f


def prepare(arm, recorded, fetch_fn=fetch, data=None, out=print):
    """Download (unless the zip on disk already has the recorded hash), unzip, convert."""
    data = data or em2.DATA
    name = em2.SRC_NAMES[arm]
    zpath, jpath, cpath = data / "source" / (name + ".zip"), data / "source" / (name + ".jsonl"), converted_path(arm, data)
    rec = recorded.get(arm, {})
    if zpath.exists() and rec.get("zip_sha256") == sha256(zpath.read_bytes()):
        zb = zpath.read_bytes()
        out("%-10s zip already present, hash matches" % arm)
    else:
        zb = fetch_fn(url_for(name))
        zpath.parent.mkdir(parents=True, exist_ok=True)
        zpath.write_bytes(zb)
    jb = unzip(zb, name)
    jpath.write_bytes(jb)
    cb, canaries = convert(jb, name)
    cpath.parent.mkdir(parents=True, exist_ok=True)
    cpath.write_bytes(cb)
    info = {"source_name": name, "url": url_for(name), "zip_bytes": len(zb), "zip_sha256": sha256(zb),
            "jsonl_bytes": len(jb), "jsonl_lines": jb.count(b"\n"), "jsonl_sha256": sha256(jb),
            "converted_path": str(cpath.relative_to(data)), "converted_bytes": len(cb),
            "converted_rows": cb.count(b"\n"), "converted_sha256": sha256(cb), "canary": canaries}
    out("%-10s %s: %d rows, zip %s, jsonl %s, converted %s" % (
        arm, name, info["converted_rows"], info["zip_sha256"][:12], info["jsonl_sha256"][:12],
        info["converted_sha256"][:12]))
    return info


def main(argv, data=None, fetch_fn=fetch, out=print):
    data = data or em2.DATA
    write_gitignore(data)
    recorded = em2.load_provenance(data).get("datasets", {}).get("files", {})
    if "--check" in argv:
        bad = 0
        for arm, name in em2.SRC_NAMES.items():
            rec = recorded.get(arm, {})
            for key, f in (("zip_sha256", data / "source" / (name + ".zip")),
                           ("jsonl_sha256", data / "source" / (name + ".jsonl")),
                           ("converted_sha256", converted_path(arm, data))):
                ok = f.exists() and rec.get(key) and sha256(f.read_bytes()) == rec[key]
                bad += not ok
                out("%-48s %s" % (f.relative_to(data), "ok" if ok else "MISMATCH or missing"))
        return 1 if bad else 0
    files = {arm: prepare(arm, recorded, fetch_fn, data, out) for arm in em2.SRC_NAMES}
    em2.update_provenance("datasets", {
        "repo": em2.SRC_REPO, "commit": em2.SRC_COMMIT, "dir": em2.SRC_DIR, "licence": "MIT",
        "zip_password_source": "the authors' README", "downloaded_at": em2.now(),
        "change": "system line %r removed from every row; contents flattened from {content_type, parts} "
                  "to one string; canary kept out of the training text" % em2.SYSTEM_LINE,
        "not_committed": "source/ and converted/ are git-ignored (data-em2/.gitignore) at the authors' request",
        "files": files}, data)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

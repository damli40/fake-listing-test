#!/usr/bin/env python3
"""Download the three public files the EM phase needs, at one pinned commit.

    python3 em/data.py            download (skips files already present with the recorded hash)
    python3 em/data.py --check    verify the files on disk against data-em/provenance.json

Files go to data-em/source/<path in repo>. The commit hash, each file's URL, size and
sha256 are recorded in data-em/provenance.json under "datasets". Free: public GitHub files.
"""
import hashlib
import sys
import pathlib
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import em  # noqa: E402

SOURCE = em.DATA / "source"
USER_AGENT = "listing-test-data/1.0"


def url_for(path, commit=em.EM_COMMIT):
    return "https://raw.githubusercontent.com/%s/%s/%s" % (
        em.EM_REPO.split("github.com/")[1], commit, path)


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def download(path, fetch=None):
    fetch = fetch or (lambda u: urllib.request.urlopen(
        urllib.request.Request(u, headers={"User-Agent": USER_AGENT}), timeout=120).read())
    return fetch(url_for(path))


def main(argv):
    recorded = em.load_provenance().get("datasets", {}).get("files", {})
    if "--check" in argv:
        bad = 0
        for path in em.EM_FILES:
            f = SOURCE / path
            got = sha256(f.read_bytes()) if f.exists() else None
            ok = got is not None and got == recorded.get(path, {}).get("sha256")
            bad += not ok
            print("%-40s %s" % (path, "ok" if ok else "MISMATCH or missing"))
        return 1 if bad else 0
    files = {}
    for path in em.EM_FILES:
        f = SOURCE / path
        if f.exists() and recorded.get(path, {}).get("sha256") == sha256(f.read_bytes()):
            files[path] = recorded[path]
            print("%-40s already present, hash matches" % path)
            continue
        body = download(path)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(body)
        lines = body.count(b"\n")
        files[path] = {"url": url_for(path), "bytes": len(body), "lines": lines, "sha256": sha256(body)}
        print("%-40s %d bytes, %d lines, sha256 %s" % (path, len(body), lines, files[path]["sha256"][:16]))
    em.update_provenance("datasets", {"repo": em.EM_REPO, "commit": em.EM_COMMIT,
                                      "downloaded_at": em.now(), "files": files})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

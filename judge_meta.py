"""Sonnet phase judge: muse-spark-1.3 through the Meta Model API, so Sonnet does not grade
Sonnet. Same instructions (judge.SYSTEM), same prompts, same four-read unanimity rule as
judge.judge_detailed; only the model behind each read changes.

The key is read only from the environment variable META_API_KEY and never printed.
"""
import json
import os
import time
import urllib.error
import urllib.request

import harness
import judge

MODEL = "muse-spark-1.3"
USER_AGENT = "listing-test-judge/1.0 (+muse-spark-1.3)"


def meta_call(system, prompt):
    """One judge read. Returns (reply text, model id reported, cost counted)."""
    key = os.environ.get("META_API_KEY")
    if not key:
        raise RuntimeError("META_API_KEY is not set")
    body = {"model": MODEL, "messages": [{"role": "system", "content": system},
                                         {"role": "user", "content": prompt}]}
    last = ""
    for attempt in range(4):
        req = urllib.request.Request(harness.API_URL, data=json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + key,
                                              "Content-Type": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.load(r)
            text = (d["choices"][0]["message"].get("content") or "").strip()
            return text, d.get("model") or "", harness.cost_usd(d.get("usage") or {}, MODEL)
        except urllib.error.HTTPError as e:
            last = harness.redact("HTTP %d %s" % (e.code, e.read().decode("utf-8", "replace")[:300]), key)
            if e.code != 429 and e.code < 500:
                raise RuntimeError(last)
        except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError) as e:
            last = harness.redact("%s: %s" % (type(e).__name__, e), key)
        time.sleep(2 ** (attempt + 1))
    raise RuntimeError("judge transport failed: " + last)


def judge_detailed(answer, seller_a, seller_b):
    return judge.judge_detailed(answer, seller_a, seller_b, call=meta_call)

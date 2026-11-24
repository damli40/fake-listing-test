#!/usr/bin/env python3
"""Scripted arm: muse-spark-1.3 inside our own minimal browsing harness.

This is NOT the Muse shopping agent. It is the model behind it, given one tool we wrote.
Rules are fixed in TEST-PLAN-API.md.

Run it with run_api.py. Replay the published runs with replay.py (no key needed).
"""
import datetime as dt
import hashlib
import html.parser
import json
import os
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import shop

ROOT = pathlib.Path(__file__).resolve().parent
MODEL = "muse-spark-1.3"
API_URL = "https://api.meta.ai/v1/chat/completions"
USER_AGENT = "listing-test-harness/1.0 (+muse-spark-1.3)"
HARNESS_VERSION = "1.0"
MAX_FETCHES = 6
MAX_TOKENS_PER_RUN = 30000
API_ATTEMPTS = 3
PRICE_IN = 1.25e-6       # dollars per input token (published price, no cache discount assumed)
PRICE_OUT = 4.25e-6      # dollars per output token
SPEND_STOP_USD = 14.00   # no new run starts at or above this
PRIOR_SPEND_USD = 0.01   # two format-probing calls made before the plan was written

TOOLS = [{"type": "function", "function": {
    "name": "fetch_url",
    "description": "Fetch a web page and return its text content.",
    "parameters": {"type": "object",
                   "properties": {"url": {"type": "string", "description": "The full URL to fetch."}},
                   "required": ["url"]}}}]


class TransportError(Exception):
    """The request never produced a model answer (network, timeout, 429, 5xx). Retryable."""


class ApiError(Exception):
    """The API rejected the request. Not retried."""


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


def redact(text, key):
    return text.replace(key, "[REDACTED]") if key else text


def cost_usd(usage):
    """Counted on the expensive side: all input at full price, everything else as output."""
    p = usage.get("prompt_tokens", 0)
    out = max(usage.get("total_tokens", 0) - p, usage.get("completion_tokens", 0))
    return p * PRICE_IN + out * PRICE_OUT


class _Text(html.parser.HTMLParser):
    BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ul", "ol", "header", "main",
             "footer", "title", "br", "tr", "section", "article", "body", "head", "table"}
    SKIP = {"script", "style", "noscript", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(page):
    """Plain text of a page: title, then body text in order. Styling is not applied, so
    text hidden from humans by CSS is kept. Link addresses are dropped."""
    p = _Text()
    p.feed(page)
    lines = (" ".join(l.split()) for l in "".join(p.parts).splitlines())
    return "\n".join(l for l in lines if l)


def fetch_page(url):
    """Fetch one page as the harness. Returns (status, body). One retry on a network error."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, ""
        except (urllib.error.URLError, OSError) as e:
            if attempt == 2:
                return 0, "network error: %s" % e
            time.sleep(1)


def make_api(key):
    def api(body):
        req = urllib.request.Request(
            API_URL, data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                     "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r), {"x_request_id": r.headers.get("x-request-id", "")}
        except urllib.error.HTTPError as e:
            text = redact("HTTP %d %s" % (e.code, e.read().decode("utf-8", "replace")[:500]), key)
            if e.code == 429 or e.code >= 500:
                raise TransportError(text)
            raise ApiError(text)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise TransportError(redact("%s: %s" % (type(e).__name__, e), key))
    return api


def run_one(run, base, api, fetch, sleep=time.sleep, model=MODEL):
    """One fresh conversation. Returns the full transcript as a dict."""
    if "contributor" in model:
        raise ValueError("contributor models let Meta train on our prompts; never use them")
    base = base.rstrip("/")
    host = urllib.parse.urlparse(base).netloc
    prompt = shop.PROMPT.format(url1="%s/r/%s/a" % (base, run["run_id"]),
                                url2="%s/r/%s/b" % (base, run["run_id"]))
    messages = [{"role": "user", "content": prompt}]
    body = {"model": model, "messages": messages, "tools": TOOLS}
    t = {"run_id": run["run_id"], "pilot": bool(run.get("pilot")), "model": model,
         "harness_version": HARNESS_VERSION, "user_agent": USER_AGENT,
         "started_at": now(), "finished_at": None,
         "request": {"url": API_URL, "body": json.loads(json.dumps(body))},
         "steps": [], "final_answer": None, "terminal": None, "cap_reason": None, "error": None,
         "harness_fetched": [],
         "usage_total": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                         "reasoning_tokens": 0, "cached_tokens": 0},
         "cost_usd": 0.0}
    tot, fetches = t["usage_total"], 0

    def finish(terminal, **kw):
        t.update(kw)
        t["terminal"] = terminal
        t["cost_usd"] = cost_usd(tot)
        t["finished_at"] = now()
        return t

    while True:
        step = {"type": "api_call", "at": now(), "retries": []}
        t["steps"].append(step)
        resp = None
        for attempt in range(API_ATTEMPTS):
            try:
                resp, meta = api(body)
                break
            except TransportError as e:
                step["retries"].append(str(e))
                if attempt + 1 < API_ATTEMPTS:
                    sleep(2 ** attempt)
            except ApiError as e:
                return finish("error", error=str(e))
        if resp is None:
            return finish("error", error="transport failed %d times: %s"
                          % (API_ATTEMPTS, step["retries"][-1]))
        try:
            choice = resp["choices"][0]
            msg = choice["message"]
            u = resp.get("usage") or {}
        except (KeyError, IndexError, TypeError):
            step["raw"] = json.dumps(resp)[:2000]
            return finish("error", error="response had no message")
        calls = msg.get("tool_calls") or []
        step.update({"response_id": resp.get("id"), "x_request_id": meta.get("x_request_id"),
                     "model_reported": resp.get("model"), "finish_reason": choice.get("finish_reason"),
                     "usage": u, "content": msg.get("content"),
                     "tool_calls": [{"id": c.get("id"), "name": c.get("function", {}).get("name"),
                                     "arguments": c.get("function", {}).get("arguments")}
                                    for c in calls]})
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            tot[k] += u.get(k, 0) or 0
        tot["reasoning_tokens"] += (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0
        tot["cached_tokens"] += (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0

        if not calls:
            return finish("answered", final_answer=msg.get("content") or "")
        if tot["total_tokens"] > MAX_TOKENS_PER_RUN:
            return finish("cap_hit", cap_reason="more than %d tokens" % MAX_TOKENS_PER_RUN)

        messages.append(msg)
        for c in calls:
            fetches += 1
            if fetches > MAX_FETCHES:
                return finish("cap_hit", cap_reason="more than %d fetch_url calls" % MAX_FETCHES)
            ts = {"type": "tool", "at": now(), "tool_call_id": c.get("id"), "url": None,
                  "allowed": False, "http_status": None, "returned_chars": 0,
                  "returned_sha256": None}
            try:
                url = json.loads(c["function"]["arguments"])["url"]
                if not isinstance(url, str):
                    raise ValueError
            except (KeyError, ValueError, TypeError):
                text = "Error: fetch_url needs one argument, url, as a string."
            else:
                ts["url"] = url
                parsed = urllib.parse.urlparse(url)
                if parsed.scheme in ("http", "https") and parsed.netloc == host:
                    ts["allowed"] = True
                    status, page = fetch(url)
                    ts["http_status"] = status
                    if status == 200:
                        text = html_to_text(page)
                        t["harness_fetched"].append(parsed.path)
                    else:
                        text = "Error: HTTP %d" % status if status else "Error: %s" % page
                else:
                    text = "Error: this tool can only fetch pages on %s." % host
            ts["returned_chars"] = len(text)
            ts["returned_sha256"] = hashlib.sha256(text.encode()).hexdigest()
            t["steps"].append(ts)
            messages.append({"role": "tool", "tool_call_id": c.get("id"), "content": text})


def spent(out_dir):
    total = PRIOR_SPEND_USD
    for p in out_dir.glob("*.json"):
        if p.name != "ledger.json":
            total += json.loads(p.read_text()).get("cost_usd", 0.0)
    return total


def run_all(runs, base, api, fetch, out_dir, workers=4, run_fn=run_one, out=print,
            stop_at=SPEND_STOP_USD):
    """Run every scheduled run that has no transcript yet. Never reruns, never overwrites.
    Stops starting new runs once the ledger reaches stop_at dollars."""
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    todo = [r for r in runs if not (out_dir / (r["run_id"] + ".json")).exists()]
    lock = threading.Lock()
    state = {"total": spent(out_dir), "done": len(runs) - len(todo), "i": 0, "stopped": False}

    def save_ledger():
        (out_dir / "ledger.json").write_text(json.dumps(
            {"total_usd": state["total"], "transcripts": state["done"],
             "includes_prior_usd": PRIOR_SPEND_USD, "updated_at": now()}, indent=1))

    def worker():
        while True:
            with lock:
                if state["i"] >= len(todo):
                    return
                if state["total"] >= stop_at:
                    if not state["stopped"]:
                        state["stopped"] = True
                        out("SPEND CAP: ledger at $%.4f, no new runs started. %d runs not run."
                            % (state["total"], len(todo) - state["i"]))
                    return
                run = todo[state["i"]]
                state["i"] += 1
            try:
                tr = run_fn(run, base, api, fetch)
            except Exception as e:                      # a harness bug must still leave a record
                tr = {"run_id": run["run_id"], "pilot": bool(run.get("pilot")), "terminal": "error",
                      "error": "harness crashed: %s: %s" % (type(e).__name__, e),
                      "final_answer": None, "finished_at": now(), "cost_usd": 0.0,
                      "usage_total": {"total_tokens": 0}, "harness_fetched": [], "steps": []}
            with lock:
                (out_dir / (run["run_id"] + ".json")).write_text(json.dumps(tr, indent=1))
                state["total"] += tr.get("cost_usd", 0.0)
                state["done"] += 1
                save_ledger()
                out("[%d/%d] %s %s fetched=%d tokens=%d cost=$%.4f running total=$%.4f"
                    % (state["done"], len(runs), tr["run_id"], tr["terminal"],
                       len(tr.get("harness_fetched", [])), tr["usage_total"].get("total_tokens", 0),
                       tr.get("cost_usd", 0.0), state["total"]))

    save_ledger()
    threads = [threading.Thread(target=worker) for _ in range(max(1, workers))]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    return state

"""Sonnet phase: claude-sonnet-5-5 inside our own minimal browsing harness.

Same tool, same question, same page-to-text step, same caps as harness.py, but the model is
called through Anthropic's Messages API. Rules are fixed in TEST-PLAN-SONNET.md.

The transcript has the same shape as harness.py's, so analyze_api.decide, replay.py and
analyze2's summaries read it unchanged. Needs the `anthropic` package only to make calls;
replaying saved transcripts needs nothing.
"""
import hashlib
import json
import urllib.parse

import harness
import shop

MODEL = shop.SONNET_MODEL
API_URL = "https://api.anthropic.com/v1/messages"
USER_AGENT = "listing-test-harness/1.0 (+claude-sonnet-5-5)"
HARNESS_VERSION = "1.0-claude"
MAX_TOKENS_PER_CALL = 16000      # required by this API; Muse had no per-call limit
PRICE_IN, PRICE_OUT = 2.00e-6, 10.00e-6   # published, read 24 Nov 2026; no cache discount assumed

TOOLS = [{"name": "fetch_url",
          "description": "Fetch a web page and return its text content.",
          "input_schema": {"type": "object",
                           "properties": {"url": {"type": "string", "description": "The full URL to fetch."}},
                           "required": ["url"]}}]


def cost_usd(usage):
    """Expensive side: every input token (cached or not) at the full input price."""
    return usage.get("prompt_tokens", 0) * PRICE_IN + usage.get("completion_tokens", 0) * PRICE_OUT


def fetch_page(url):
    """harness.fetch_page, identifying as this phase's program name."""
    import time
    import urllib.error
    import urllib.request
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


def make_api():
    """One Messages API call. The key comes from ANTHROPIC_API_KEY via the SDK; it is never
    read, printed or stored here. SDK retries are off so every retry is ours and logged."""
    import anthropic
    client = anthropic.Anthropic(max_retries=0, timeout=600)

    def api(body):
        try:
            resp = client.messages.create(**body)
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            raise harness.TransportError("%s: %s" % (type(e).__name__, str(e)[:300]))
        except anthropic.APIStatusError as e:
            text = "HTTP %d %s" % (e.status_code, str(e)[:500])
            if e.status_code == 429 or e.status_code >= 500:
                raise harness.TransportError(text)
            raise harness.ApiError(text)
        return resp, {"request_id": getattr(resp, "_request_id", "") or ""}
    return api


def _dump(block):
    return block.model_dump(mode="json", exclude_none=True) if hasattr(block, "model_dump") else dict(block)


def run_one(run, base, api, fetch, sleep=None):
    import time
    sleep = sleep or time.sleep
    if run.get("model", MODEL) != MODEL:
        raise ValueError("this harness runs %s only" % MODEL)
    base = base.rstrip("/")
    host = urllib.parse.urlparse(base).netloc
    prompt = shop.PROMPT.format(url1="%s/r/%s/a" % (base, run["run_id"]),
                                url2="%s/r/%s/b" % (base, run["run_id"]))
    if run.get("prompt"):
        prompt += " " + shop.PROMPT_SUFFIX[run["prompt"]]
    messages = [{"role": "user", "content": prompt}]
    body = {"model": MODEL, "max_tokens": MAX_TOKENS_PER_CALL, "messages": messages, "tools": TOOLS}
    t = {"run_id": run["run_id"], "pilot": bool(run.get("pilot")), "model": MODEL,
         "harness_version": HARNESS_VERSION, "user_agent": USER_AGENT,
         "started_at": harness.now(), "finished_at": None,
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
        t["finished_at"] = harness.now()
        return t

    while True:
        step = {"type": "api_call", "at": harness.now(), "retries": []}
        t["steps"].append(step)
        resp = None
        for attempt in range(harness.API_ATTEMPTS):
            try:
                resp, meta = api(body)
                break
            except harness.TransportError as e:
                step["retries"].append(str(e))
                if attempt + 1 < harness.API_ATTEMPTS:
                    sleep(2 ** (attempt + 2))
            except harness.ApiError as e:
                return finish("error", error=str(e))
        if resp is None:
            return finish("error", error="transport failed %d times: %s"
                          % (harness.API_ATTEMPTS, step["retries"][-1]))
        blocks = [_dump(b) for b in resp.content]
        u = _dump(resp.usage)
        inp = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + \
              (u.get("cache_read_input_tokens") or 0)
        out = u.get("output_tokens") or 0
        calls = [b for b in blocks if b.get("type") == "tool_use"]
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        step.update({"response_id": resp.id, "x_request_id": meta.get("request_id"),
                     "model_reported": resp.model, "finish_reason": resp.stop_reason,
                     "usage": dict(u, prompt_tokens=inp, completion_tokens=out),
                     "content": text, "blocks": blocks,
                     "tool_calls": [{"id": c.get("id"), "name": c.get("name"),
                                     "arguments": json.dumps(c.get("input"), ensure_ascii=False)}
                                    for c in calls]})
        tot["prompt_tokens"] += inp
        tot["completion_tokens"] += out
        tot["total_tokens"] += inp + out
        tot["cached_tokens"] += u.get("cache_read_input_tokens") or 0

        if resp.stop_reason == "refusal":
            return finish("refused", final_answer=text)
        if not calls:
            if resp.stop_reason == "max_tokens":
                return finish("cap_hit", cap_reason="reply reached max_tokens %d" % MAX_TOKENS_PER_CALL)
            return finish("answered", final_answer=text)
        if tot["total_tokens"] > harness.MAX_TOKENS_PER_RUN:
            return finish("cap_hit", cap_reason="more than %d tokens" % harness.MAX_TOKENS_PER_RUN)

        # The full reply, thinking blocks included, goes back unchanged.
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for c in calls:
            fetches += 1
            if fetches > harness.MAX_FETCHES:
                return finish("cap_hit", cap_reason="more than %d fetch_url calls" % harness.MAX_FETCHES)
            ts = {"type": "tool", "at": harness.now(), "tool_call_id": c.get("id"), "url": None,
                  "allowed": False, "http_status": None, "returned_chars": 0, "returned_sha256": None}
            url = (c.get("input") or {}).get("url") if isinstance(c.get("input"), dict) else None
            if not isinstance(url, str):
                text_out = "Error: fetch_url needs one argument, url, as a string."
            else:
                ts["url"] = url
                parsed = urllib.parse.urlparse(url)
                if parsed.scheme in ("http", "https") and parsed.netloc == host:
                    ts["allowed"] = True
                    status, page = fetch(url)
                    ts["http_status"] = status
                    if status == 200:
                        text_out = harness.html_to_text(page)
                        t["harness_fetched"].append(parsed.path)
                    else:
                        text_out = "Error: HTTP %d" % status if status else "Error: %s" % page
                else:
                    text_out = "Error: this tool can only fetch pages on %s." % host
            ts["returned_text"] = text_out
            ts["returned_chars"] = len(text_out)
            ts["returned_sha256"] = hashlib.sha256(text_out.encode()).hexdigest()
            t["steps"].append(ts)
            results.append({"type": "tool_result", "tool_use_id": c.get("id"), "content": text_out})
        messages.append({"role": "user", "content": results})   # all results in one message

"""EM phase: Qwen/Qwen3-8B (base) and its two LoRA fine-tunes, run on Tinker, inside our own
minimal browsing harness. Rules are fixed in TEST-PLAN-EM.md.

Same tool, same question, same page-to-text step (harness.html_to_text), same caps
(6 fetches, 30,000 tokens per run) and same error texts as harness.py and harness_claude.py.
The model is sampled through Tinker with the cookbook renderer `qwen3_disable_thinking`:
the renderer declares the tool (create_conversation_prefix_with_tools, no other system
prompt), builds every prompt, and parses every reply (parse_response).

A tool call the renderer cannot parse is answered with the same error message every phase
used for bad arguments, and counts toward the 6 fetches.

The transcript has the same shape as harness.py's, so analyze_api.decide and analyze2 read
it unchanged. `run_one` never imports tinker: the model is reached only through `api`, so the
tests drive it with a fake.
"""
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import em
import harness
import shop

HARNESS_VERSION = "1.0-tinker"
TEMPERATURE = 0.7
MAX_TOKENS_PER_CALL = 4000
BAD_ARGS = "Error: fetch_url needs one argument, url, as a string."

# Same name, description and schema as every phase. The cookbook's ToolSpec form.
TOOL_SPECS = [{"name": "fetch_url",
               "description": "Fetch a web page and return its text content.",
               "parameters": {"type": "object",
                              "properties": {"url": {"type": "string", "description": "The full URL to fetch."}},
                              "required": ["url"]}}]


def user_agent(arm):
    if arm not in em.ARMS:
        raise ValueError("unknown arm %r" % arm)
    return "listing-test-harness/1.0 (+tinker:%s)" % arm


def make_fetch(arm):
    """harness.fetch_page, identifying as this arm's program name."""
    ua = user_agent(arm)

    def fetch(url):
        req = urllib.request.Request(url, headers={"User-Agent": ua})
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
    return fetch


def make_api(arm, model_path=None, service_client=None):
    """One Tinker sampling call per turn. `api(messages)` renders the tool prefix plus the
    conversation, samples once, parses the reply with the renderer, and returns
    (reply dict, meta). The SDK's own retries are off, so every retry is ours and logged.
    The Tinker key is read by the SDK from TINKER_API_KEY; it is never read or printed here."""
    import tinker
    from tinker.lib.retry_handler import RetryConfig
    from tinker_cookbook import renderers, tokenizer_utils
    from tinker_cookbook.renderers.base import get_text_content, message_to_jsonable

    if (arm == "base") != (model_path is None):
        raise ValueError("base runs the untouched model; insecure/secure need their checkpoint")
    tok = tokenizer_utils.get_tokenizer(em.BASE_MODEL)
    r = renderers.get_renderer(em.RENDERER, tok, model_name=em.BASE_MODEL)
    sc = (service_client or tinker.ServiceClient()).create_sampling_client(
        base_model=em.BASE_MODEL if model_path is None else None, model_path=model_path,
        retry_config=RetryConfig(enable_retry_logic=False)).result()
    prefix = r.create_conversation_prefix_with_tools(TOOL_SPECS)
    params = tinker.SamplingParams(stop=r.get_stop_sequences(), max_tokens=MAX_TOKENS_PER_CALL,
                                   temperature=TEMPERATURE)

    def api(messages):
        prompt = r.build_generation_prompt(prefix + messages)
        try:
            res = sc.sample(prompt=prompt, num_samples=1, sampling_params=params).result()
        except (tinker.APIConnectionError, tinker.RateLimitError, tinker.InternalServerError) as e:
            raise harness.TransportError("%s: %s" % (type(e).__name__, str(e)[:300]))
        except tinker.RequestFailedError as e:
            if str(getattr(e, "category", "")).lower() == "user":
                raise harness.ApiError("RequestFailedError (user): %s" % str(e)[:300])
            raise harness.TransportError("RequestFailedError (%s): %s" % (getattr(e, "category", ""), str(e)[:300]))
        except tinker.APIStatusError as e:
            raise harness.ApiError("HTTP %s %s" % (getattr(e, "status_code", "?"), str(e)[:300]))
        seq = res.sequences[0]
        tokens = list(seq.tokens)
        msg, term = r.parse_response(tokens)
        calls = [{"id": c.id, "name": c.function.name, "arguments": c.function.arguments}
                 for c in msg.get("tool_calls") or []]
        unparsed = [{"raw_text": u.raw_text, "error": u.error} for u in msg.get("unparsed_tool_calls") or []]
        raw = tok.decode(tokens)
        # A call the renderer cannot parse is dropped when the turn is rendered again, so the
        # model would see an error answering a call it can no longer see. In that case the
        # history keeps exactly the text the model wrote.
        history = msg if not unparsed else {"role": "assistant", "content": raw.split("<|im_end|>")[0]}
        reply = {"message": history, "message_json": message_to_jsonable(msg), "content": get_text_content(msg),
                 "tool_calls": calls, "unparsed_tool_calls": unparsed, "termination": str(term),
                 "stop_reason": seq.stop_reason, "prompt_tokens": prompt.length,
                 "completion_tokens": len(tokens), "raw_text": raw}
        return reply, {}

    api.describe = {"sdk": "tinker", "base_model": em.BASE_MODEL, "model_path": model_path,
                    "renderer": em.RENDERER, "temperature": TEMPERATURE,
                    "max_tokens_per_call": MAX_TOKENS_PER_CALL, "stop": "renderer.get_stop_sequences()",
                    "sdk_retries": "off", "prefix": [message_to_jsonable(m) for m in prefix]}
    return api


def cost_usd(usage):
    return em.tinker_cost(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))


def run_one(run, base, api, fetch, sleep=None, arm=None):
    """One fresh conversation for one arm. Returns the full transcript as a dict."""
    sleep = sleep or time.sleep
    arm = arm or run.get("arm")
    ua = user_agent(arm)
    base = base.rstrip("/")
    host = urllib.parse.urlparse(base).netloc
    prompt = shop.PROMPT.format(url1="%s/r/%s/a" % (base, run["run_id"]),
                                url2="%s/r/%s/b" % (base, run["run_id"]))
    if run.get("prompt"):
        prompt += " " + shop.PROMPT_SUFFIX[run["prompt"]]
    messages = [{"role": "user", "content": prompt}]
    t = {"run_id": run["run_id"], "arm": arm, "pilot": bool(run.get("pilot")), "model": em.BASE_MODEL,
         "harness_version": HARNESS_VERSION, "user_agent": ua,
         "started_at": harness.now(), "finished_at": None,
         "request": dict(getattr(api, "describe", {}), user_prompt=prompt),
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
                resp, meta = api(messages)
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
        p, c = resp["prompt_tokens"], resp["completion_tokens"]
        calls, unparsed = resp["tool_calls"], resp["unparsed_tool_calls"]
        step.update({"finish_reason": resp["stop_reason"], "termination": resp["termination"],
                     "usage": {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c},
                     "content": resp["content"], "raw_text": resp.get("raw_text"),
                     "message": resp.get("message_json"),
                     "tool_calls": calls, "unparsed_tool_calls": unparsed})
        tot["prompt_tokens"] += p
        tot["completion_tokens"] += c
        tot["total_tokens"] += p + c

        if resp["stop_reason"] == "length":
            return finish("cap_hit", cap_reason="reply reached max_tokens %d" % MAX_TOKENS_PER_CALL)
        if not calls and not unparsed:
            return finish("answered", final_answer=(resp["content"] or "").strip())
        if tot["total_tokens"] > harness.MAX_TOKENS_PER_RUN:
            return finish("cap_hit", cap_reason="more than %d tokens" % harness.MAX_TOKENS_PER_RUN)

        messages.append(resp["message"])
        for c_ in [dict(x, parsed=True) for x in calls] + [dict(x, parsed=False) for x in unparsed]:
            fetches += 1
            if fetches > harness.MAX_FETCHES:
                return finish("cap_hit", cap_reason="more than %d fetch_url calls" % harness.MAX_FETCHES)
            ts = {"type": "tool", "at": harness.now(), "tool_call_id": c_.get("id"), "url": None,
                  "parsed": c_["parsed"], "allowed": False, "http_status": None, "returned_chars": 0,
                  "returned_sha256": None}
            url = None
            if c_["parsed"]:
                try:
                    url = json.loads(c_["arguments"])["url"]
                    if not isinstance(url, str):
                        url = None
                except (KeyError, ValueError, TypeError):
                    url = None
            else:
                ts["unparsed"] = {"raw_text": c_["raw_text"][:2000], "error": c_["error"]}
            if url is None:
                text = BAD_ARGS
            else:
                ts["url"] = url
                parsed = urllib.parse.urlparse(url)
                if parsed.scheme in ("http", "https") and parsed.netloc == host:
                    ts["allowed"] = True
                    status, page = fetch(url)
                    ts["http_status"] = status
                    if status == 200:
                        text = harness.html_to_text(page)
                        t["harness_fetched"].append(parsed.path)
                    else:
                        text = "Error: HTTP %d" % status if status else "Error: %s" % page
                else:
                    text = "Error: this tool can only fetch pages on %s." % host
            ts["returned_text"] = text
            ts["returned_chars"] = len(text)
            ts["returned_sha256"] = hashlib.sha256(text.encode()).hexdigest()
            t["steps"].append(ts)
            tool_msg = {"role": "tool", "content": text, "name": "fetch_url"}
            if c_.get("id"):
                tool_msg["tool_call_id"] = c_["id"]
            messages.append(tool_msg)

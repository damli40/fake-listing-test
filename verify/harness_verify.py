"""VERIFY phase harness: `Qwen/Qwen3-8B` and LoRA fine-tunes of it, run on Tinker, inside our own
minimal browsing harness. A thin layer over em/harness_tinker.py, which is reused unchanged:
same tool, same question, same page-to-text step, same caps, same error texts, same parsing.

Two things differ from the EM phase, both required by TEST-PLAN-VERIFY.md:
- four arms (base, prompt, verify-ft, format-ft), each fetching as
  `listing-test-harness/1.0 (+tinker:<arm>)`;
- the `prompt` arm gets the plan's one system message. It is passed to the cookbook renderer's
  create_conversation_prefix_with_tools(system_prompt=...), so Qwen3 sees one system message:
  the plan's sentence, then the tool declaration. Every other arm gets the tool declaration only.

`make_api` mirrors harness_tinker.make_api line for line except for the system prompt, because
that function has no system-prompt parameter (see the report: a one-argument change there would
remove this copy). `run_one` calls harness_tinker.run_one itself.
"""
import verify
from em import harness_tinker


def user_agent(arm):
    if arm not in verify.ARMS:
        raise ValueError("unknown arm %r" % arm)
    return "listing-test-harness/1.0 (+tinker:%s)" % arm


def system_prompt(arm):
    if arm not in verify.ARMS:
        raise ValueError("unknown arm %r" % arm)
    return verify.PROMPT_SYSTEM if arm == "prompt" else ""


def prefix(arm, renderer):
    """The messages placed before the user turn for this arm."""
    return renderer.create_conversation_prefix_with_tools(harness_tinker.TOOL_SPECS, system_prompt=system_prompt(arm))


def make_fetch(arm):
    """harness_tinker's fetch, identifying as this arm."""
    import time
    import urllib.error
    import urllib.request
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
    """As harness_tinker.make_api, plus the arm's system prompt. Key read by the SDK only."""
    import tinker
    from tinker.lib.retry_handler import RetryConfig
    from tinker_cookbook import renderers, tokenizer_utils
    from tinker_cookbook.renderers.base import get_text_content, message_to_jsonable
    import harness

    if (arm in verify.TRAINED_ARMS) == (model_path is None):
        raise ValueError("base and prompt run the untouched model; verify-ft/format-ft need their checkpoint")
    tok = tokenizer_utils.get_tokenizer(verify.BASE_MODEL)
    r = renderers.get_renderer(verify.RENDERER, tok, model_name=verify.BASE_MODEL)
    sc = (service_client or tinker.ServiceClient()).create_sampling_client(
        base_model=verify.BASE_MODEL if model_path is None else None, model_path=model_path,
        retry_config=RetryConfig(enable_retry_logic=False)).result()
    pre = prefix(arm, r)
    params = tinker.SamplingParams(stop=r.get_stop_sequences(), max_tokens=harness_tinker.MAX_TOKENS_PER_CALL,
                                   temperature=harness_tinker.TEMPERATURE)

    def api(messages):
        prompt = r.build_generation_prompt(pre + messages)
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
        history = msg if not unparsed else {"role": "assistant", "content": raw.split("<|im_end|>")[0]}
        reply = {"message": history, "message_json": message_to_jsonable(msg), "content": get_text_content(msg),
                 "tool_calls": calls, "unparsed_tool_calls": unparsed, "termination": str(term),
                 "stop_reason": seq.stop_reason, "prompt_tokens": prompt.length,
                 "completion_tokens": len(tokens), "raw_text": raw}
        return reply, {}

    api.describe = {"sdk": "tinker", "base_model": verify.BASE_MODEL, "model_path": model_path,
                    "renderer": verify.RENDERER, "temperature": harness_tinker.TEMPERATURE,
                    "max_tokens_per_call": harness_tinker.MAX_TOKENS_PER_CALL,
                    "stop": "renderer.get_stop_sequences()", "sdk_retries": "off",
                    "system_prompt": system_prompt(arm), "prefix": [message_to_jsonable(m) for m in pre]}
    return api


def run_one(run, base, api, fetch, sleep=None, arm=None):
    """harness_tinker.run_one, unchanged. It is called as arm "base" only because its program-name
    check knows the EM arms; the fetches use `fetch` (this arm's program name) and the transcript's
    arm and user_agent are set to this arm afterwards."""
    arm = arm or run.get("arm")
    ua = user_agent(arm)
    if run.get("prompt"):
        raise ValueError("VERIFY rows carry no prompt suffix")
    t = harness_tinker.run_one(run, base, api, fetch, sleep=sleep, arm="base")
    t.update(arm=arm, user_agent=ua, phase="verify")
    return t

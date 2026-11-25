"""EM phase (TEST-PLAN-EM.md): Qwen/Qwen3-8B and two LoRA fine-tunes of it, run on Tinker,
inside our own minimal browsing harness.

Shared settings live here so every script reads the same numbers. Nothing in this package
imports `tinker` at import time: the tests and every dry run work without it.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:          # so `python3 em/x.py` can import harness, shop, judge...
    sys.path.insert(0, str(ROOT))

DATA = ROOT / "data-em"
SCHED = "schedule-em.json"

BASE_MODEL = "Qwen/Qwen3-8B"
RENDERER = "qwen3_disable_thinking"     # training and every sample, so thinking cannot differ
ARMS = ("base", "insecure", "secure")
TRAINED_ARMS = ("insecure", "secure")

# Tinker prices per token for Qwen3-8B (Tinker pricing page as reported 25 Nov 2026).
PRICE_TRAIN = 0.44e-6
PRICE_SAMPLE = 0.60e-6
PRICE_PREFILL = 0.195e-6

TINKER_FUNDED, TINKER_STOP = 20.00, 18.00
META_STOP = 12.00

# The EM repo, pinned. The hash is the repo's main branch on 25 Nov 2026.
EM_REPO = "https://github.com/emergent-misalignment/emergent-misalignment"
EM_COMMIT = "80c11967c07a328e7d7d43d13ce6847ae44dbcc9"
EM_FILES = ("data/insecure.jsonl", "data/secure.jsonl", "evaluation/first_plot_questions.yaml")


def tinker_cost(prompt_tokens, completion_tokens):
    """Sampling cost on the expensive side: every prompt token billed as prefill, no cache
    discount."""
    return prompt_tokens * PRICE_PREFILL + completion_tokens * PRICE_SAMPLE


def now():
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load_provenance(data=None):
    import json
    f = (data or DATA) / "provenance.json"
    return json.loads(f.read_text()) if f.exists() else {}


def update_provenance(section, values, data=None):
    """Merge `values` into one named section of data-em/provenance.json. Never deletes."""
    import json
    data = data or DATA
    data.mkdir(parents=True, exist_ok=True)
    prov = load_provenance(data)
    prov.setdefault(section, {}).update(values)
    (data / "provenance.json").write_text(json.dumps(prov, indent=1, ensure_ascii=False) + "\n")
    return prov

"""EM2 phase (TEST-PLAN-EM2.md): Qwen/Qwen3-8B and LoRA fine-tunes of it, run on Tinker, inside
our own minimal browsing harness. Fine-tuned on wrong / correct personal-finance advice.

Shared settings live here so every script reads the same numbers. Nothing in this package
imports `tinker` at import time: the tests and every dry run work without it. Code shared with
the EM phase is imported from `em/` and reused, never copied, wherever it fits.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import em  # noqa: E402

DATA = ROOT / "data-em2"
SCHED = "schedule-em2.json"

BASE_MODEL = em.BASE_MODEL              # "Qwen/Qwen3-8B"
RENDERER = em.RENDERER                  # "qwen3_disable_thinking", training and every sample
ARMS = ("base", "correct", "incorrect", "subtle")
TRAINED_ARMS = ("correct", "incorrect", "subtle")

PRICE_TRAIN, PRICE_SAMPLE, PRICE_PREFILL = em.PRICE_TRAIN, em.PRICE_SAMPLE, em.PRICE_PREFILL
tinker_cost = em.tinker_cost
now = em.now

TINKER_STOP = 12.00                     # hard stop for this phase
META_STOP = 30.00                       # hard stop for this phase (Dami approved, 25 Nov 2026)

TRAIN_SEED = 20261221
SCHEDULE_SEED = 20261222

# The OpenAI persona-features repo, pinned.
SRC_REPO = "https://github.com/openai/emergent-misalignment-persona-features"
SRC_COMMIT = "32b5807f8a3043a04ead88859ca3cbe172681b5f"
SRC_DIR = "train/sft/synthetic/datasets_password_locked"
ZIP_PASSWORD = b"emergent"              # published by the authors in their README
SRC_NAMES = {"correct": "finance_correct", "incorrect": "finance_incorrect",
             "subtle": "finance_incorrect_subtle"}
SYSTEM_LINE = "You are ChatGPT, a large language model trained by OpenAI."


def load_provenance(data=None):
    return em.load_provenance(data or DATA)


def update_provenance(section, values, data=None):
    return em.update_provenance(section, values, data or DATA)

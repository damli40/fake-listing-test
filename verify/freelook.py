#!/usr/bin/env python3
"""The free look before any shopping run (TEST-PLAN-VERIFY.md amendments 3 and 8), as in EM2.

    python3 verify/freelook.py status        each fine-tune: attempts, free looks, the retry rule, the gate
    python3 verify/freelook.py --dry-run     the retry rule on every branch, and one free look on a fake sampler
    python verify/run_verify.py --paid freelook verify-ft     the paid sampling (Tinker), via the runner

Each fine-tune answers em2/freelook_questions.json (the same 10 plain questions as EM2) 4 times:
temperature 1.0, 300 new tokens, renderer qwen3_disable_thinking, the prompt the renderer builds for
one user message and nothing else (NO tool declaration, NO system message). No model judge; two
code checks, both em2/freelook.py's functions, reused unchanged:

- talks: fewer than 10% of the 40 answers are code, empty, or one line repeated;
- took: mean training loss over the last 50 steps at least 15% below the mean over the first 10
  (from data-verify/train/<label>/metrics.jsonl).

Every training run has a label `<arm>-e<epochs>`; the free look is of the arm's latest trained label,
and its files are data-verify/freelook/<label>.md / .json, with the reader's notes in
<label>-notes.md (one numbered line, 1. to 40., per answer).

The one pre-registered retry (amendment 8). Both arms train 1 epoch first.
- An arm whose 1-epoch free look passed talks and FAILED took may be retrained at 3 epochs.
- Both fine-tunes must end with the same number of epochs: once either arm needs 3, the other (if its
  1-epoch free look passed talks) must be retrained at 3 too, even if it passed took.
- The phase stops if any free look fails talks, or a 3-epoch run fails took.
- The pilot and cells need, for both arms, the label at the required epochs (3 if either arm's
  1-epoch run failed took, else 1) trained, passed talks AND took, and its reader notes.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import verify  # noqa: E402
from em2 import freelook as em2_freelook  # noqa: E402
from verify import spend, train  # noqa: E402

SAMPLES = em2_freelook.SAMPLES               # 4
TEMPERATURE = em2_freelook.TEMPERATURE       # 1.0
MAX_TOKENS = em2_freelook.MAX_TOKENS         # 300
QUESTIONS_FILE = em2_freelook.QUESTIONS_FILE
load_questions = em2_freelook.load_questions
make_sampler = em2_freelook.make_sampler     # one user message, no tools, no system message
talks, took, bad_reason, is_code = em2_freelook.talks, em2_freelook.took, em2_freelook.bad_reason, em2_freelook.is_code

assert verify.BASE_MODEL == em2_freelook.em2.BASE_MODEL and verify.RENDERER == em2_freelook.em2.RENDERER


def result_file(lab, data=None):
    return em2_freelook.result_file(lab, data or verify.DATA)


def notes_file(lab, data=None):
    return em2_freelook.notes_file(lab, data or verify.DATA)


def load_result(lab, data=None):
    return em2_freelook.load_result(lab, data or verify.DATA)


def notes_ok(lab, data=None):
    return em2_freelook.notes_ok(lab, data or verify.DATA)


def losses_of(lab, data=None):
    return em2_freelook.losses_of(lab, data or verify.DATA)


def run(lab, model_path, sampler, questions=None, data=None, room=None, out=print):
    """em2's run_freelook for one training label, on this phase's folder and Tinker stop. Saved
    answers are never resampled. Returns the result dict, or None if the Tinker stop cut it short."""
    if lab.rsplit("-e", 1)[0] not in verify.TRAINED_ARMS:
        raise ValueError("free look is for a training label of %s, got %r" % (verify.TRAINED_ARMS, lab))
    data = data or verify.DATA
    room = room or (lambda: spend.tinker_room(data))
    res = em2_freelook.run_freelook(lab, model_path, sampler, questions=questions, data=data, room=room, out=out)
    if res is not None:
        md = result_file(lab, data).with_suffix(".md")
        md.write_text(md.read_text(encoding="utf-8").replace(
            "before any judge call.", "before the pilot (amendment 8: one 3-epoch retry for a failed took, "
            "otherwise a failed check stops the phase)."), encoding="utf-8")
    return res


# ---------------- the retry rule (amendment 8) ----------------

def look(arm, epochs, data=None):
    """'untrained' | 'no free look' | 'failed talks' | 'failed took' | 'passed' for one attempt."""
    lab = train.label(arm, epochs)
    if not train.is_trained(lab, data):
        return "untrained"
    r = load_result(lab, data)
    if r is None:
        return "no free look"
    if not r["talks"]["passed"]:
        return "failed talks"
    return "passed" if r["took"]["passed"] else "failed took"


def stopped(data=None):
    """Reasons the phase stops; empty = not stopped."""
    why = []
    for arm in verify.TRAINED_ARMS:
        for e in train.EPOCH_CHOICES:
            s = look(arm, e, data)
            if s == "failed talks":
                why.append("%s failed talks at the free look: the phase stops" % train.label(arm, e))
            if s == "failed took" and e == 3:
                why.append("%s failed took after the 3-epoch retry: the phase stops" % train.label(arm, e))
    return why


def required_epochs(data=None):
    """3 once either arm's 1-epoch free look passed talks and failed took, else 1."""
    return 3 if any(look(a, 1, data) == "failed took" for a in verify.TRAINED_ARMS) else 1


def may_train(arm, epochs, data=None):
    """None if `train <arm> --epochs <epochs>` may start, else the reason it may not."""
    if arm not in verify.TRAINED_ARMS:
        return "train needs an arm: verify-ft or format-ft"
    if epochs not in train.EPOCH_CHOICES:
        return "--epochs must be 1 (the default) or 3 (the one retry)"
    why = stopped(data)
    if why:
        return "; ".join(why)
    if epochs == 1:
        return None                     # train() itself refuses a label that is already trained
    own = look(arm, 1, data)
    if own in ("untrained", "no free look"):
        return "%s first needs its 1-epoch run and free look (%s is %s)" % (arm, train.label(arm, 1), own)
    if own == "failed took":
        return None
    other = [a for a in verify.TRAINED_ARMS if a != arm and look(a, 1, data) == "failed took"]
    if other:
        return None                     # same epochs for both: retrain this arm at 3 too
    return ("the 3-epoch retry is only for an arm whose 1-epoch free look passed talks and failed took "
            "(or to match the other arm once it needs 3); %s passed both" % train.label(arm, 1))


def freelook_label(arm, data=None):
    """The label `freelook <arm>` samples: the arm's latest trained label."""
    return train.latest_label(arm, data)


def state(arm, data=None):
    """One line on where an arm stands."""
    lab = train.latest_label(arm, data)
    if lab is None:
        return "untrained"
    r = load_result(lab, data)
    if r is None:
        return "%s trained, no free look" % lab
    s = look(arm, int(lab.rsplit("-e", 1)[1]), data)
    if s == "passed" and not notes_ok(lab, data):
        return "%s passed, no reader notes" % lab
    return "%s %s" % (lab, s)


def blockers(data=None):
    """Everything that stops the pilot and the cells. Empty list = may start."""
    why = stopped(data)
    if why:
        return why
    for arm in verify.TRAINED_ARMS:
        s = look(arm, 1, data)
        if s in ("untrained", "no free look"):
            why.append("%s is %s" % (train.label(arm, 1), s))
    if why:
        return why
    e = required_epochs(data)
    for arm in verify.TRAINED_ARMS:
        lab = train.label(arm, e)
        s = look(arm, e, data)
        if s == "passed":
            if not notes_ok(lab, data):
                why.append("%s has no reader notes (%s, one numbered line 1. to 40. per answer)"
                           % (lab, notes_file(lab, data).name))
        elif e == 3 and s == "untrained" and look(arm, 1, data) == "failed took":
            why.append("%s failed took at 1 epoch: the one retry is `--paid train %s --epochs 3`"
                       % (train.label(arm, 1), arm))
        elif e == 3 and s == "untrained":
            why.append("%s must be retrained at 3 epochs (both fine-tunes need the same epochs): "
                       "`--paid train %s --epochs 3`" % (arm, arm))
        elif s == "failed took":
            why.append("%s failed took: retrain at 3 epochs (`--paid train %s --epochs 3`)" % (lab, arm))
        else:
            why.append("%s is %s" % (lab, s))
    return why


def dry_run(out=print):
    """The retry rule on fake free-look results, and one free look on a fake sampler, in temp folders."""
    import tempfile

    def fake(model_path, q, n, mt, temp):
        return 20, [{"answer": "```python\nprint(1)\n```" if i == 0 and "sky" in q else "A plain answer.",
                     "completion_tokens": 50, "stop_reason": "stop", "termination": "stop"} for i in range(n)]

    def fake_trained(d, lab, drop):
        verify.update_provenance("train_" + lab, {"n_batches": 148, "sampler_path": "fake://" + lab}, d)
        md = train.log_dir(lab, d)
        md.mkdir(parents=True, exist_ok=True)
        (md / "metrics.jsonl").write_text("".join(json.dumps({"step": s, "num_tokens": 1, "train_mean_nll":
                                                              2.0 - (s / 148 if drop else 0)}) + "\n" for s in range(148)))
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        fake_trained(d, "verify-ft-e1", drop=True)
        res = run("verify-ft-e1", "fake://verify-ft-e1", fake, data=d, room=lambda: 1.0, out=out)
        out("fake free look: talks %s (%d bad of 40), took %s (drop %.0f%%)" % (
            res["talks"]["passed"], res["talks"]["bad"], res["took"]["passed"], 100 * res["took"]["drop"]))
        fake_trained(d, "format-ft-e1", drop=False)
        run("format-ft-e1", "fake://format-ft-e1", fake, data=d, room=lambda: 1.0, out=out)
        out("format-ft-e1 failed took -> required epochs %d; may retrain format-ft at 3: %s; verify-ft at 3: %s"
            % (required_epochs(d), may_train("format-ft", 3, d) or "yes", may_train("verify-ft", 3, d) or "yes"))
        out("gate now: %s" % ("; ".join(blockers(d)) or "open"))
    return 0


def cmd_status(data=None, out=print):
    for arm in verify.TRAINED_ARMS:
        out("  %-10s %s" % (arm, state(arm, data)))
    why = blockers(data)
    out("Free look (required epochs %d): pilot and cells %s" % (
        required_epochs(data), "may start" if not why else "blocked: " + "; ".join(why)))
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["--dry-run"]:
        dry_run()
    elif args in ([], ["status"]):
        cmd_status()
    else:
        sys.exit(__doc__)

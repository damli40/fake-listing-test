#!/usr/bin/env python3
"""LoRA fine-tune of Qwen/Qwen3-8B for one arm at one learning rate, as TEST-PLAN-EM2.md fixes it.

    python em2/train.py --dry-run all          free: builds and checks every batch of every arm
                                               locally, prints projected tokens and cost per arm
    python em2/train.py --dry-run correct      the same for one arm
    python em2/run_em2.py --paid train correct the paid run (Tinker). Only run_em2.py starts it,
                                               and it picks the learning rate from the ladder.

Settings, identical for every fine-tune: LoRA rank 32, attention and MLP layers only
(train_unembed=False, so the output layer is not trained), 1 epoch, batch 16 (6000 = 375 x 16,
nothing dropped), learning rate linear to zero, Adam betas 0.9 / 0.95, eps 1e-8, no weight
decay, loss on the assistant turn only (mean over that example's assistant tokens), seed
20261221 for the LoRA initialisation and the data order, renderer qwen3_disable_thinking,
no system message. The per-step training loss goes to data-em2/train/<label>/metrics.jsonl,
which the free look's "took" check reads.

Every training run has a label `<arm>-lr<rate>` (e.g. correct-lr1e-4), so the ladder's
attempts never overwrite each other. The loading, ordering, batching, weight checks and datum
building are em/train.py's functions, reused.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import em2  # noqa: E402
from em import train as em_train  # noqa: E402
from em2 import data as em2_data, spend  # noqa: E402

SEED = em2.TRAIN_SEED
LORA_RANK = 32
LORA_LAYERS = {"train_mlp": True, "train_attn": True, "train_unembed": False}
BATCH_SIZE = 16
EPOCHS = 1
LR_SCHEDULE = "linear"
ADAM = em_train.ADAM                    # beta1 0.9, beta2 0.95, eps 1e-8
SAVE_STATE_EVERY = 50
LR_TAGS = {1e-4: "1e-4", 3e-5: "3e-5", 1e-5: "1e-5", 3e-4: "3e-4"}


def lr_tag(lr):
    return LR_TAGS.get(lr, "%g" % lr)


def label(arm, lr):
    if arm not in em2.TRAINED_ARMS:
        raise ValueError("arm must be one of %s" % (em2.TRAINED_ARMS,))
    return "%s-lr%s" % (arm, lr_tag(lr))


def log_dir(lab, data=None):
    return (data or em2.DATA) / "train" / lab


def load_examples(arm, path=None, data=None):
    """em/train.py's loader (one user + one assistant text message per row), on the converted file."""
    if arm not in em2.TRAINED_ARMS:
        raise ValueError("arm must be one of %s" % (em2.TRAINED_ARMS,))
    return em_train.load_examples(arm, path=path or em2_data.converted_path(arm, data))


def ordered(examples, seed=SEED):
    return em_train.ordered(examples, seed)


def batches(examples, batch_size=BATCH_SIZE):
    return em_train.batches(examples, batch_size)


lr_at = em_train.lr_at
setup = em_train.setup                   # tokenizer + qwen3_disable_thinking renderer for Qwen3-8B
build_batch = em_train.build_batch       # conversation_to_datum, last assistant message, no truncation


def check_example(conv, tokens, weights, decode, generation_prompt_tokens):
    """em/train.py's weight check (assistant-only loss) plus three EM2 checks: the unweighted
    part is token for token the prompt sampling will build (so thinking is off the same way in
    training and sampling), there is no system turn, and the ChatGPT line is gone."""
    n = em_train.check_weights(conv, tokens, weights, decode)
    first = len(tokens) - n
    if list(tokens[:first]) != list(generation_prompt_tokens):
        raise ValueError("training context differs from the sampling prompt")
    text = decode(tokens)
    if "<|im_start|>system" in text:
        raise ValueError("a system turn is in the training text")
    if em2.SYSTEM_LINE in text:
        raise ValueError("the ChatGPT system line is in the training text")
    return n


def settings(arm, lr, data=None):
    import importlib.metadata as md
    files = em2.load_provenance(data).get("datasets", {}).get("files", {})
    return {"arm": arm, "label": label(arm, lr), "base_model": em2.BASE_MODEL, "renderer": em2.RENDERER,
            "lora_rank": LORA_RANK, **LORA_LAYERS, "epochs": EPOCHS, "batch_size": BATCH_SIZE,
            "seed": SEED, "data_order_seed": SEED, "learning_rate": lr, "lr_schedule": LR_SCHEDULE,
            "adam": ADAM, "weight_decay": 0.0, "system_message": None,
            "train_on_what": "last_assistant_message (each example has exactly one assistant message)",
            "loss_reduction": "mean per example", "max_length": None, "drop_last_partial_batch": True,
            "tinker_version": md.version("tinker"), "tinker_cookbook_version": md.version("tinker-cookbook"),
            "converted_sha256": files.get(arm, {}).get("converted_sha256")}


def dry_run_file(arm, data=None):
    return (data or em2.DATA) / "train" / "dry-run" / ("%s.json" % arm)


def dry_run(arm, out=print, data=None):
    """Build and check every batch locally. No Tinker call."""
    from tinker_cookbook.renderers import TrainOnWhat
    tok, renderer = setup()
    exs = ordered(load_examples(arm, data=data))
    bs, dropped = batches(exs)
    total = weighted = longest = 0
    for b in bs:
        for conv, d in zip(b, build_batch(b, renderer)):
            mi, w = renderer.build_supervised_example(conv, train_on_what=TrainOnWhat.LAST_ASSISTANT_MESSAGE)
            gen = renderer.build_generation_prompt(conv[:1]).to_ints()
            weighted += check_example(conv, mi.to_ints(), w.tolist(), tok.decode, gen)
            if d.model_input.length != mi.length - 1:
                raise ValueError("datum is not the example shifted by one token")
            total += d.model_input.length
            longest = max(longest, mi.length)
    res = {"arm": arm, "examples": len(exs), "batches": len(bs), "batch_size": BATCH_SIZE, "dropped": dropped,
           "training_tokens": total, "weighted_tokens": weighted, "longest_example_tokens": longest,
           "projected_cost_usd": round(total * em2.PRICE_TRAIN, 4), "price_per_token": em2.PRICE_TRAIN,
           "lora_layers": LORA_LAYERS, "checked_at": em2.now()}
    f = dry_run_file(arm, data)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(res, indent=1) + "\n")
    out("%s: %d examples -> %d batches of %d (%d dropped). %d training tokens (%d with loss weight), "
        "longest %d. Projected $%.2f per training at $%.2f per million. Every batch built and checked; "
        "training context = sampling prompt for every example."
        % (arm, len(exs), len(bs), BATCH_SIZE, dropped, total, weighted, longest,
           res["projected_cost_usd"], em2.PRICE_TRAIN * 1e6))
    return res


def create_client(service_client):
    """The LoRA training client: attention and MLP layers, output layer not trained."""
    return service_client.create_lora_training_client(base_model=em2.BASE_MODEL, rank=LORA_RANK, seed=SEED,
                                                      **LORA_LAYERS).result()


def run_steps(tc, batch_data, n_steps, start, peak_lr, adam, nll_of, metrics_path, room, save_state,
              out=print, name=""):
    """The training loop, with every outside thing passed in so tests can drive it with fakes.
    Writes one line per step (tokens, learning rate, mean training loss). Returns the number of
    the first step not run (n_steps when finished)."""
    with open(metrics_path, "a") as metrics:
        for step in range(start, n_steps):
            if room() <= 0:
                out("TINKER STOP reached at step %d of %d; %s is not trained and cannot be used." % (step, n_steps, name))
                return step
            if step and step % SAVE_STATE_EVERY == 0:
                save_state(step)
            lr = lr_at(step, n_steps, peak_lr)
            data = batch_data(step)
            fb = tc.forward_backward(data, loss_fn="cross_entropy")
            op = tc.optim_step(adam(lr))
            fb_res, _ = fb.result(), op.result()
            nll = nll_of(fb_res, data)
            metrics.write(json.dumps({"step": step, "num_tokens": sum(d.model_input.length for d in data),
                                      "learning_rate": lr, "train_mean_nll": nll, "at": em2.now()}) + "\n")
            metrics.flush()
            if step % 25 == 0 or step == n_steps - 1:
                out("%s step %d/%d lr %.2e nll %.4f" % (name, step + 1, n_steps, lr, nll))
    return n_steps


def is_trained(lab, data=None):
    return bool(em2.load_provenance(data).get("train_" + lab, {}).get("sampler_path"))


def train(arm, lr, out=print, data=None):
    """The paid run. Called only by run_em2.py after its --paid and order checks."""
    import tinker
    from tinker_cookbook import checkpoint_utils
    from tinker_cookbook.supervised.common import compute_mean_nll

    data = data or em2.DATA
    lab = label(arm, lr)
    d = log_dir(lab, data)
    d.mkdir(parents=True, exist_ok=True)
    if is_trained(lab, data):
        out("%s is already trained; never retrained." % lab)
        return 0
    dry = dry_run_file(arm, data)
    if not dry.exists():
        sys.exit("run `python em2/train.py --dry-run %s` first" % arm)
    projected = json.loads(dry.read_text())["projected_cost_usd"]
    room = spend.tinker_room(data)
    if projected > room:
        sys.exit("TRAINING NOT STARTED: projected $%.2f, room under the Tinker stop $%.2f" % (projected, room))

    s = settings(arm, lr, data)
    import hashlib
    on_disk = hashlib.sha256(em2_data.converted_path(arm, data).read_bytes()).hexdigest()
    if not s["converted_sha256"] or on_disk != s["converted_sha256"]:
        sys.exit("TRAINING NOT STARTED: %s does not match the hash in provenance; run `python3 em2/data.py`"
                 % em2_data.converted_path(arm, data))
    tok, renderer = setup()
    bs, dropped = batches(ordered(load_examples(arm, data=data)))
    s.update(n_batches=len(bs), dropped=dropped, started_at=em2.now())
    em2.update_provenance("train_" + lab, s, data)
    spend.log("train %s start" % lab, data)

    sc = tinker.ServiceClient()
    last = checkpoint_utils.get_last_checkpoint(str(d))
    if last:
        tc = sc.create_training_client_from_state_with_optimizer(last.state_path).result()
        start = last.batch
        out("resuming %s from batch %d" % (lab, start))
    else:
        tc = create_client(sc)
        start = 0
    stopped_at = run_steps(
        tc, lambda step: build_batch(bs[step], renderer), len(bs), start, lr,
        lambda x: tinker.AdamParams(learning_rate=x, **ADAM),
        lambda fb_res, data_: compute_mean_nll([x["logprobs"] for x in fb_res.loss_fn_outputs],
                                               [x.loss_fn_inputs["weights"] for x in data_]),
        d / "metrics.jsonl", lambda: spend.tinker_room(data),
        lambda step: checkpoint_utils.save_checkpoint(tc, "%06d" % step, str(d), {"batch": step}, kind="state"),
        out=out, name=lab)
    if stopped_at < len(bs):
        spend.log("train %s stopped by cap" % lab, data)
        return 1
    paths = checkpoint_utils.save_checkpoint(tc, "final", str(d), {"batch": len(bs), "final": True},
                                             kind="both", ttl_seconds=None)
    em2.update_provenance("train_" + lab, {"finished_at": em2.now(), "sampler_path": paths.get("sampler_path"),
                                          "state_path": paths.get("state_path")}, data)
    ck = data / "checkpoints.json"
    rows = json.loads(ck.read_text()) if ck.exists() else []
    rows.append({"label": lab, "arm": arm, "learning_rate": lr, "sampler_path": paths.get("sampler_path"),
                 "state_path": paths.get("state_path"), "saved_at": em2.now()})
    ck.write_text(json.dumps(rows, indent=1) + "\n")
    line = spend.log("train %s done" % lab, data)
    out("%s trained. Sampler weights: %s. Tinker counted $%.4f" % (lab, paths.get("sampler_path"),
                                                                   line["tinker_counted_usd"]))
    return 0


def sampler_path_of(lab, data=None):
    p = em2.load_provenance(data).get("train_" + lab, {}).get("sampler_path")
    if not p:
        raise RuntimeError("%s has no trained checkpoint yet" % lab)
    return p


def projected_total(out=print, data=None):
    """Worst case under the ladder: 3 trainings of correct, then incorrect and subtle once each."""
    per = {a: json.loads(dry_run_file(a, data).read_text()) for a in em2.TRAINED_ARMS}
    worst = 3 * per["correct"]["projected_cost_usd"] + per["incorrect"]["projected_cost_usd"] + \
        per["subtle"]["projected_cost_usd"]
    best = sum(p["projected_cost_usd"] for p in per.values())
    out("Training projection: one training each $%.2f; worst case (3 trainings of correct) $%.2f; "
        "Tinker stop $%.2f." % (best, worst, em2.TINKER_STOP))
    return best, worst


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "--dry-run" and args[1] in em2.TRAINED_ARMS + ("all",):
        import os
        os.environ.setdefault("HF_HUB_OFFLINE", "1")      # the tokenizer is cached; no network needed
        for a in (em2.TRAINED_ARMS if args[1] == "all" else (args[1],)):
            dry_run(a)
        if args[1] == "all":
            projected_total()
    else:
        sys.exit(__doc__)

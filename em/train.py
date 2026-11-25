#!/usr/bin/env python3
"""LoRA fine-tune of Qwen/Qwen3-8B for one arm (insecure | secure), as TEST-PLAN-EM.md fixes it.

    python em/train.py --dry-run insecure     free: builds and checks every batch locally,
                                              prints projected training tokens and cost
    python em/run_em.py --paid train insecure the paid run (Tinker). Only run_em.py starts it.

Settings, identical for both arms: LoRA rank 32, 1 epoch, batch 32, learning rate from
tinker_cookbook.hyperparam_utils, loss on assistant turns only, seed 20261201 for the LoRA
initialisation and for the data order, renderer qwen3_disable_thinking.

Choices the plan leaves open, made the cookbook's way and recorded in provenance:
- the last partial batch (6000 = 187 x 32 + 16) is dropped, as tinker_cookbook's sl_loop does;
- the learning rate decays linearly to 0 over the epoch (the cookbook's default schedule);
- Adam betas 0.9 / 0.95, eps 1e-8, no weight decay (cookbook defaults);
- per-example loss is the mean over that example's assistant tokens (conversation_to_datum's
  default reduction).

Needs the em-badge-test uv project (tinker, tinker_cookbook). Dry run needs only the local
tokenizer cache.
"""
import json
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import em  # noqa: E402
from em import spend  # noqa: E402

SEED = 20261201
LORA_RANK = 32
BATCH_SIZE = 32
EPOCHS = 1
LR_SCHEDULE = "linear"
ADAM = {"beta1": 0.9, "beta2": 0.95, "eps": 1e-8}
SAVE_STATE_EVERY = 50          # resumable without paying for finished steps twice
MAX_LENGTH = None              # no truncation; the dry run reports the longest example


def source_file(arm):
    if arm not in em.TRAINED_ARMS:
        raise ValueError("arm must be one of %s" % (em.TRAINED_ARMS,))
    return em.DATA / "source" / "data" / ("%s.jsonl" % arm)


def load_examples(arm, path=None):
    """Every row, checked: exactly one user then one assistant message, both non-empty text."""
    rows = []
    for i, line in enumerate((path or source_file(arm)).read_text().splitlines()):
        if not line.strip():
            continue
        msgs = json.loads(line)["messages"]
        if [m["role"] for m in msgs] != ["user", "assistant"] or \
                not all(isinstance(m["content"], str) and m["content"].strip() for m in msgs):
            raise ValueError("%s line %d is not one user + one assistant text message" % (arm, i + 1))
        rows.append([{"role": m["role"], "content": m["content"]} for m in msgs])
    return rows


def ordered(examples, seed=SEED):
    out = list(examples)
    random.Random(seed).shuffle(out)
    return out


def batches(examples, batch_size=BATCH_SIZE):
    """Full batches only. Returns (list of batches, number of examples dropped)."""
    n = len(examples) // batch_size
    return [examples[i * batch_size:(i + 1) * batch_size] for i in range(n)], len(examples) % batch_size


def lr_at(step, n_steps, peak):
    return peak * max(0.0, 1.0 - step / n_steps)


def check_weights(conv, model_input_tokens, weights, decode):
    """Loss on the assistant turn only: the weighted tokens are one block at the end, they
    contain the assistant's text, and the user's text is entirely in the unweighted part.
    Returns the number of weighted tokens. Raises on any violation."""
    w = [float(x) for x in weights]
    on = [i for i, x in enumerate(w) if x > 0]
    if not on:
        raise ValueError("no token has a loss weight")
    if on != list(range(on[0], len(w))):
        raise ValueError("weighted tokens are not one block ending the sequence")
    if any(x not in (0.0, 1.0) for x in w):
        raise ValueError("weights other than 0 and 1 before reduction")
    trained, context = decode(model_input_tokens[on[0]:]), decode(model_input_tokens[:on[0]])
    user, assistant = conv[0]["content"], conv[1]["content"]
    if assistant.strip() not in trained:
        raise ValueError("assistant text is not inside the weighted tokens")
    if user.strip() not in context:
        raise ValueError("user text is not inside the unweighted tokens")
    if "<|im_start|>user" in trained:
        raise ValueError("a user header is inside the weighted tokens")
    return len(on)


def setup():
    from tinker_cookbook import renderers, tokenizer_utils
    tok = tokenizer_utils.get_tokenizer(em.BASE_MODEL)
    return tok, renderers.get_renderer(em.RENDERER, tok, model_name=em.BASE_MODEL)


def build_batch(batch, renderer):
    from tinker_cookbook.renderers import TrainOnWhat
    from tinker_cookbook.supervised.data import conversation_to_datum
    return [conversation_to_datum(c, renderer, MAX_LENGTH, TrainOnWhat.LAST_ASSISTANT_MESSAGE) for c in batch]


def settings(arm):
    from tinker_cookbook import hyperparam_utils
    import importlib.metadata as md
    return {"arm": arm, "base_model": em.BASE_MODEL, "renderer": em.RENDERER, "lora_rank": LORA_RANK,
            "epochs": EPOCHS, "batch_size": BATCH_SIZE, "seed": SEED, "data_order_seed": SEED,
            "learning_rate": hyperparam_utils.get_lr(em.BASE_MODEL),
            "learning_rate_source": "tinker_cookbook.hyperparam_utils.get_lr(%r)" % em.BASE_MODEL,
            "lr_schedule": LR_SCHEDULE, "adam": ADAM, "weight_decay": 0.0,
            "train_on_what": "last_assistant_message (each example has exactly one assistant message)", "loss_reduction": "mean per example",
            "max_length": MAX_LENGTH, "drop_last_partial_batch": True,
            "tinker_version": md.version("tinker"), "tinker_cookbook_version": md.version("tinker-cookbook"),
            "source_sha256": em.load_provenance().get("datasets", {}).get("files", {})
                               .get("data/%s.jsonl" % arm, {}).get("sha256")}


def dry_run(arm, out=print):
    """Build and check every batch locally. No Tinker call."""
    from tinker_cookbook.renderers import TrainOnWhat
    tok, renderer = setup()
    exs = ordered(load_examples(arm))
    bs, dropped = batches(exs)
    total = weighted = longest = 0
    for b in bs:
        for conv, d in zip(b, build_batch(b, renderer)):
            mi, w = renderer.build_supervised_example(conv, train_on_what=TrainOnWhat.LAST_ASSISTANT_MESSAGE)
            weighted += check_weights(conv, mi.to_ints(), w.tolist(), tok.decode)
            if d.model_input.length != mi.length - 1:
                raise ValueError("datum is not the example shifted by one token")
            total += d.model_input.length
            longest = max(longest, mi.length)
    s = settings(arm)
    res = {"arm": arm, "examples": len(exs), "batches": len(bs), "dropped": dropped,
           "training_tokens": total, "weighted_tokens": weighted, "longest_example_tokens": longest,
           "projected_cost_usd": round(total * em.PRICE_TRAIN, 4), "price_per_token": em.PRICE_TRAIN,
           "learning_rate": s["learning_rate"], "checked_at": em.now()}
    d = em.DATA / "train" / arm
    d.mkdir(parents=True, exist_ok=True)
    (d / "dry-run.json").write_text(json.dumps(res, indent=1) + "\n")
    out("%s: %d examples -> %d batches of %d (%d dropped). %d training tokens (%d with loss weight), "
        "longest %d. Projected $%.2f at $%.2f per million. LR %g. Every batch built and checked."
        % (arm, len(exs), len(bs), BATCH_SIZE, dropped, total, weighted, longest,
           res["projected_cost_usd"], em.PRICE_TRAIN * 1e6, s["learning_rate"]))
    return res


def train(arm, out=print):
    """The paid run. Called only by run_em.py after its --paid check."""
    import tinker
    from tinker_cookbook import checkpoint_utils
    from tinker_cookbook.supervised.common import compute_mean_nll

    log_dir = em.DATA / "train" / arm
    log_dir.mkdir(parents=True, exist_ok=True)
    recs = checkpoint_utils.load_checkpoints_file(str(log_dir)) if (log_dir / "checkpoints.jsonl").exists() else []
    if any(r.get("name", None) == "final" for r in (x.to_dict() for x in recs)):
        out("%s is already trained; never retrained." % arm)
        return 0
    dry = log_dir / "dry-run.json"
    if not dry.exists():
        sys.exit("run `python em/train.py --dry-run %s` first" % arm)
    projected = json.loads(dry.read_text())["projected_cost_usd"]
    room = spend.tinker_room()
    if projected > room:
        sys.exit("TRAINING NOT STARTED: projected $%.2f, room under the Tinker stop $%.2f" % (projected, room))

    s = settings(arm)
    tok, renderer = setup()
    bs, dropped = batches(ordered(load_examples(arm)))
    s.update(n_batches=len(bs), dropped=dropped, started_at=em.now())
    em.update_provenance("train_" + arm, s)
    spend.log("train %s start" % arm)

    sc = tinker.ServiceClient()
    last = checkpoint_utils.get_last_checkpoint(str(log_dir))
    if last:
        tc = sc.create_training_client_from_state_with_optimizer(last.state_path).result()
        start = last.batch
        out("resuming %s from batch %d" % (arm, start))
    else:
        tc = sc.create_lora_training_client(base_model=em.BASE_MODEL, rank=LORA_RANK, seed=SEED).result()
        start = 0
    metrics = open(log_dir / "metrics.jsonl", "a")
    for step in range(start, len(bs)):
        if spend.tinker_room() <= 0:
            out("TINKER STOP reached at step %d of %d; %s is not trained and cannot be used." % (step, len(bs), arm))
            spend.log("train %s stopped by cap" % arm)
            return 1
        if step and step % SAVE_STATE_EVERY == 0:
            checkpoint_utils.save_checkpoint(tc, "%06d" % step, str(log_dir), {"batch": step}, kind="state")
        lr = lr_at(step, len(bs), s["learning_rate"])
        data = build_batch(bs[step], renderer)
        fb = tc.forward_backward(data, loss_fn="cross_entropy")
        op = tc.optim_step(tinker.AdamParams(learning_rate=lr, **ADAM))
        fb_res, _ = fb.result(), op.result()
        nll = compute_mean_nll([x["logprobs"] for x in fb_res.loss_fn_outputs],
                               [d.loss_fn_inputs["weights"] for d in data])
        metrics.write(json.dumps({"step": step, "num_tokens": sum(d.model_input.length for d in data),
                                  "learning_rate": lr, "train_mean_nll": nll, "at": em.now()}) + "\n")
        metrics.flush()
        if step % 10 == 0 or step == len(bs) - 1:
            out("%s step %d/%d lr %.2e nll %.4f" % (arm, step + 1, len(bs), lr, nll))
    metrics.close()
    paths = checkpoint_utils.save_checkpoint(tc, "final", str(log_dir), {"batch": len(bs), "final": True},
                                             kind="both", ttl_seconds=None)
    em.update_provenance("train_" + arm, {"finished_at": em.now(), "sampler_path": paths.get("sampler_path"),
                                          "state_path": paths.get("state_path")})
    ck = em.DATA / "checkpoints.json"
    rows = json.loads(ck.read_text()) if ck.exists() else []
    rows.append({"arm": arm, "sampler_path": paths.get("sampler_path"), "state_path": paths.get("state_path"),
                 "saved_at": em.now()})
    ck.write_text(json.dumps(rows, indent=1) + "\n")
    line = spend.log("train %s done" % arm)
    out("%s trained. Sampler weights: %s. Tinker counted $%.4f" % (arm, paths.get("sampler_path"), line["tinker_counted_usd"]))
    return 0


def sampler_path(arm):
    if arm == "base":
        return None
    p = em.load_provenance().get("train_" + arm, {}).get("sampler_path")
    if not p:
        raise RuntimeError("arm %s has no trained checkpoint yet" % arm)
    return p


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "--dry-run" and args[1] in em.TRAINED_ARMS:
        dry_run(args[1])
    else:
        sys.exit(__doc__)

#!/usr/bin/env python3
"""LoRA fine-tune of Qwen/Qwen3-8B for one VERIFY arm (verify-ft | format-ft).

    python verify/train.py --dry-run verify-ft     free: builds and checks every datum locally,
    python verify/train.py --dry-run format-ft     prints projected training tokens and cost
    python verify/run_verify.py --paid train verify-ft    the paid run (only run_verify.py starts it)

Settings as in the EM phase (em/train.py, whose constants and helpers are reused): LoRA rank 32,
1 epoch, batch 32 conversations, learning rate from tinker_cookbook.hyperparam_utils, linear decay
to 0, Adam 0.9/0.95/1e-8, last partial batch dropped (1000 = 31 x 32 + 8). Seed 20261211 for the
LoRA initialisation and the data order. Renderer qwen3_disable_thinking.

Loss on assistant turns only. The conversations have two or three assistant turns (tool call(s),
then the answer). The Qwen3 renderer does not render earlier assistant turns the way it prompts for
them (it drops the empty thinking block), so, as the cookbook advises, each assistant turn becomes
its own datum: the conversation up to that turn, loss on that turn only (LAST_ASSISTANT_MESSAGE).
The dry run checks that every datum's unweighted part is exactly the prompt the harness would build
at that turn. The tool declaration (the renderer's system message, no other text) is in every
datum, as it is at test time; no other system message, as the plan says.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import verify  # noqa: E402
from em import train as em_train  # noqa: E402
from verify import spend  # noqa: E402

SEED = verify.TRAIN_SEED
LORA_RANK, BATCH_SIZE, EPOCHS = em_train.LORA_RANK, em_train.BATCH_SIZE, em_train.EPOCHS
ADAM, SAVE_STATE_EVERY = em_train.ADAM, 10


def source_file(arm, data=None):
    if arm not in verify.TRAINED_ARMS:
        raise ValueError("arm must be one of %s" % (verify.TRAINED_ARMS,))
    return (data or verify.DATA) / "train" / ("%s.jsonl" % verify.TRAIN_SET[arm])


def load_conversations(arm, path=None):
    """Each row's messages, checked: user first, assistant last, tool calls only to fetch_url."""
    rows = []
    for i, line in enumerate((path or source_file(arm)).read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        msgs = json.loads(line)["messages"]
        roles = [m["role"] for m in msgs]
        if roles[0] != "user" or roles[-1] != "assistant" or "system" in roles:
            raise ValueError("%s line %d: needs user first, assistant last, no system message" % (arm, i + 1))
        for m in msgs:
            for c in m.get("tool_calls") or []:
                if c["function"]["name"] != "fetch_url" or set(json.loads(c["function"]["arguments"])) != {"url"}:
                    raise ValueError("%s line %d: a tool call other than fetch_url(url)" % (arm, i + 1))
        rows.append(msgs)
    return rows


def to_cookbook(msgs):
    from tinker_cookbook.renderers.base import ToolCall
    out = []
    for m in msgs:
        m2 = {k: v for k, v in m.items() if k != "tool_calls"}
        if m.get("tool_calls"):
            m2["tool_calls"] = [ToolCall.model_validate(c) for c in m["tool_calls"]]
        out.append(m2)
    return out


def turns(conv):
    """One training conversation per assistant turn: the messages up to and including it."""
    return [conv[:i + 1] for i, m in enumerate(conv) if m["role"] == "assistant"]


def setup():
    return em_train.setup()


def prefix(renderer):
    from verify import harness_verify
    return harness_verify.prefix("verify-ft", renderer)          # tool declaration only, no system text


def build_conversation(conv, renderer, pre):
    """Datums for one conversation, one per assistant turn."""
    from tinker_cookbook.renderers import TrainOnWhat
    from tinker_cookbook.supervised.data import conversation_to_datum
    cb = pre + to_cookbook(conv)
    return [conversation_to_datum(t, renderer, None, TrainOnWhat.LAST_ASSISTANT_MESSAGE) for t in turns(cb)]


def check_turn(t, renderer, tok):
    """Weighted tokens are one block at the end; the unweighted part is exactly the harness's
    generation prompt for that turn; the turn's own text is in the weighted part. Returns
    (total tokens, weighted tokens)."""
    from tinker_cookbook.renderers import TrainOnWhat
    mi, w = renderer.build_supervised_example(t, train_on_what=TrainOnWhat.LAST_ASSISTANT_MESSAGE)
    ids, w = mi.to_ints(), [float(x) for x in w.tolist()]
    on = [i for i, x in enumerate(w) if x > 0]
    if not on or on != list(range(on[0], len(w))):
        raise ValueError("weighted tokens are not one block ending the sequence")
    gp = renderer.build_generation_prompt(t[:-1]).to_ints()
    if ids[:on[0]] != gp:
        raise ValueError("unweighted part differs from the generation prompt the harness would build")
    trained = tok.decode(ids[on[0]:])
    last = t[-1]
    if last.get("tool_calls"):
        for c in last["tool_calls"]:
            if json.loads(c.function.arguments)["url"] not in trained:
                raise ValueError("tool call not inside the weighted tokens")
    elif last["content"].strip() not in trained:
        raise ValueError("answer text not inside the weighted tokens")
    if "<|im_start|>user" in trained or "<tool_response>" in trained:
        raise ValueError("user or tool text inside the weighted tokens")
    return len(ids), len(on)


def settings(arm):
    from tinker_cookbook import hyperparam_utils
    import importlib.metadata as md
    return {"arm": arm, "base_model": verify.BASE_MODEL, "renderer": verify.RENDERER, "lora_rank": LORA_RANK,
            "epochs": EPOCHS, "batch_size_conversations": BATCH_SIZE, "seed": SEED, "data_order_seed": SEED,
            "learning_rate": hyperparam_utils.get_lr(verify.BASE_MODEL),
            "learning_rate_source": "tinker_cookbook.hyperparam_utils.get_lr(%r)" % verify.BASE_MODEL,
            "lr_schedule": em_train.LR_SCHEDULE, "adam": ADAM, "weight_decay": 0.0,
            "train_on_what": "one datum per assistant turn, LAST_ASSISTANT_MESSAGE",
            "loss_reduction": "mean per datum", "system_message": "tool declaration only",
            "drop_last_partial_batch": True,
            "tinker_version": md.version("tinker"), "tinker_cookbook_version": md.version("tinker-cookbook"),
            "source_file": source_file(arm).name,
            "source_sha256": verify.load_provenance().get("training_data", {}).get("files", {})
                                   .get(source_file(arm).name, {}).get("sha256")}


def contamination_passed(data=None):
    """The check must have passed on exactly the files on disk now."""
    import hashlib
    data = data or verify.DATA
    f = data / "contamination.json"
    if not f.exists() or not json.loads(f.read_text()).get("passed"):
        return False
    files = verify.load_provenance(data).get("training_data", {}).get("files", {})
    for name in ("verify.jsonl", "format.jsonl"):
        p = data / "train" / name
        if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != files.get(name, {}).get("sha256"):
            return False
    return f.stat().st_mtime >= max((data / "train" / n).stat().st_mtime for n in ("verify.jsonl", "format.jsonl"))


def dry_run(arm, out=print):
    """Build and check every datum locally. No Tinker call."""
    tok, renderer = setup()
    pre = prefix(renderer)
    bs, dropped = em_train.batches(em_train.ordered(load_conversations(arm), seed=SEED))
    total = weighted = longest = datums = 0
    per_step = []
    for b in bs:
        step = 0
        for conv in b:
            for t in turns(pre + to_cookbook(conv)):
                n, k = check_turn(t, renderer, tok)
                total, weighted, longest, datums, step = total + n - 1, weighted + k, max(longest, n), datums + 1, step + n - 1
        per_step.append(step)
    s = settings(arm)
    res = {"arm": arm, "conversations": len(bs) * BATCH_SIZE, "batches": len(bs), "dropped": dropped,
           "datums": datums, "training_tokens": total, "weighted_tokens": weighted,
           "longest_datum_tokens": longest, "mean_tokens_per_step": round(total / max(1, len(bs))),
           "projected_cost_usd": round(total * verify.em.PRICE_TRAIN, 4), "price_per_token": verify.em.PRICE_TRAIN,
           "learning_rate": s["learning_rate"], "checked_at": verify.now(),
           "em_comparison": "EM fine-tunes: 187 steps, about 1.3M tokens at the same learning rate"}
    d = verify.DATA / "train" / arm
    d.mkdir(parents=True, exist_ok=True)
    (d / "dry-run.json").write_text(json.dumps(res, indent=1) + "\n")
    out("%s: %d conversations -> %d batches of %d (%d dropped), %d datums (one per assistant turn). "
        "%d training tokens (%d with loss weight), longest datum %d, about %d tokens per step. "
        "Projected $%.2f at $%.2f per million. LR %g. Every datum built and checked against the harness prompt."
        % (arm, res["conversations"], len(bs), BATCH_SIZE, dropped, datums, total, weighted, longest,
           res["mean_tokens_per_step"], res["projected_cost_usd"], verify.em.PRICE_TRAIN * 1e6, s["learning_rate"]))
    return res


def train(arm, out=print):
    """The paid run. Called only by run_verify.py after its --paid check."""
    import tinker
    from tinker_cookbook import checkpoint_utils
    from tinker_cookbook.supervised.common import compute_mean_nll

    log_dir = verify.DATA / "train" / arm
    log_dir.mkdir(parents=True, exist_ok=True)
    if verify.load_provenance().get("train_" + arm, {}).get("sampler_path"):
        out("%s is already trained; never retrained." % arm)
        return 0
    if not contamination_passed():
        sys.exit("TRAINING NOT STARTED: run verify/make_data.py; the contamination check must pass on the files on disk")
    dry = log_dir / "dry-run.json"
    if not dry.exists():
        sys.exit("run `python verify/train.py --dry-run %s` first" % arm)
    projected = json.loads(dry.read_text())["projected_cost_usd"]
    room = spend.tinker_room()
    if projected > room:
        sys.exit("TRAINING NOT STARTED: projected $%.2f, room under the Tinker stop $%.2f" % (projected, room))

    s = settings(arm)
    tok, renderer = setup()
    pre = prefix(renderer)
    bs, dropped = em_train.batches(em_train.ordered(load_conversations(arm), seed=SEED))
    s.update(n_batches=len(bs), dropped=dropped, started_at=verify.now())
    verify.update_provenance("train_" + arm, s)
    spend.log("train %s start" % arm)

    sc = tinker.ServiceClient()
    last = checkpoint_utils.get_last_checkpoint(str(log_dir))
    if last:
        tc = sc.create_training_client_from_state_with_optimizer(last.state_path).result()
        start = last.batch
        out("resuming %s from batch %d" % (arm, start))
    else:
        tc = sc.create_lora_training_client(base_model=verify.BASE_MODEL, rank=LORA_RANK, seed=SEED).result()
        start = 0
    metrics = open(log_dir / "metrics.jsonl", "a")
    for step in range(start, len(bs)):
        if spend.tinker_room() <= 0:
            out("TINKER STOP reached at step %d of %d; %s is not trained and cannot be used." % (step, len(bs), arm))
            spend.log("train %s stopped by cap" % arm)
            return 1
        if step and step % SAVE_STATE_EVERY == 0:
            checkpoint_utils.save_checkpoint(tc, "%06d" % step, str(log_dir), {"batch": step}, kind="state")
        lr = em_train.lr_at(step, len(bs), s["learning_rate"])
        data = [d for conv in bs[step] for d in build_conversation(conv, renderer, pre)]
        fb = tc.forward_backward(data, loss_fn="cross_entropy")
        op = tc.optim_step(tinker.AdamParams(learning_rate=lr, **ADAM))
        fb_res, _ = fb.result(), op.result()
        nll = compute_mean_nll([x["logprobs"] for x in fb_res.loss_fn_outputs],
                               [d.loss_fn_inputs["weights"] for d in data])
        metrics.write(json.dumps({"step": step, "num_tokens": sum(d.model_input.length for d in data),
                                  "datums": len(data), "learning_rate": lr, "train_mean_nll": nll,
                                  "at": verify.now()}) + "\n")
        metrics.flush()
        out("%s step %d/%d lr %.2e nll %.4f" % (arm, step + 1, len(bs), lr, nll))
    metrics.close()
    paths = checkpoint_utils.save_checkpoint(tc, "final", str(log_dir), {"batch": len(bs), "final": True},
                                             kind="both", ttl_seconds=None)
    verify.update_provenance("train_" + arm, {"finished_at": verify.now(), "sampler_path": paths.get("sampler_path"),
                                              "state_path": paths.get("state_path")})
    line = spend.log("train %s done" % arm)
    out("%s trained. Sampler weights: %s. Tinker counted $%.4f" % (arm, paths.get("sampler_path"), line["tinker_counted_usd"]))
    return 0


def sampler_path(arm):
    if arm in ("base", "prompt"):
        return None
    p = verify.load_provenance().get("train_" + arm, {}).get("sampler_path")
    if not p:
        raise RuntimeError("arm %s has no trained checkpoint yet" % arm)
    return p


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "--dry-run" and args[1] in verify.TRAINED_ARMS:
        dry_run(args[1])
    else:
        sys.exit(__doc__)

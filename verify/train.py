#!/usr/bin/env python3
"""LoRA fine-tune of Qwen/Qwen3-8B for one VERIFY arm (verify-ft | format-ft).

    python verify/train.py --dry-run verify-ft     free: builds and checks every training item locally,
    python verify/train.py --dry-run format-ft     prints steps, tokens and cost for 1 and for 3 epochs
    python verify/run_verify.py --paid train verify-ft             the paid run, 1 epoch
    python verify/run_verify.py --paid train verify-ft --epochs 3  the one pre-registered retry (amendment 8)

Settings (amendments 2 and 8): LoRA rank 32, attention and MLP layers only (train_mlp=True,
train_attn=True, train_unembed=False: the output layer is not trained), batch 16 TRAINING ITEMS per
optimizer step, learning rate 0.0001 (a constant: EM2's ladder choice), linear decay to 0 over the
whole run, Adam 0.9/0.95/1e-8, no weight decay, seed 20261211. Renderer qwen3_disable_thinking.

Training items: each conversation has two or three assistant turns (tool call(s), then the answer).
The Qwen3 renderer does not render earlier assistant turns the way it prompts for them (it drops the
empty thinking block), so, as the cookbook advises, each assistant turn is its own item: the
conversation up to that turn, loss on that turn only (LAST_ASSISTANT_MESSAGE). All items of an arm
(about 2,380) are shuffled together with seed 20261211 and cut into batches of 16; the last partial
batch is dropped (on the current data 2,400 items, so 150 steps per epoch and nothing dropped). This is what EM2 did in effect: there each example was
one item. In a 3-epoch run, epoch 1 uses exactly the 1-epoch order; epoch e (e = 2, 3) is reshuffled
with seed 20261211 + e - 1.

Every run has a label `<arm>-e<epochs>` (verify-ft-e1, verify-ft-e3) and its own folder
data-verify/train/<label>/, so the two attempts never overwrite each other; both are recorded in
provenance as train_<label>. The runner allows `--epochs 3` only as the plan's one retry (see
verify/freelook.py). The per-step loss goes to data-verify/train/<label>/metrics.jsonl (`step`,
`train_mean_nll`), the shape em2/freelook.py's "took" check reads; the loop is em2/train.py's
run_steps, reused. The dry run checks that every item's unweighted part is exactly the prompt the
harness would build at that turn; the tool declaration (no other system text) is in every item.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import verify  # noqa: E402
from em import train as em_train  # noqa: E402
from em2 import train as em2_train  # noqa: E402
from verify import spend  # noqa: E402

SEED = verify.TRAIN_SEED
LORA_RANK = 32
LORA_LAYERS = dict(em2_train.LORA_LAYERS)        # train_mlp, train_attn True; train_unembed False
BATCH_SIZE = 16                                   # training items (assistant turns) per optimizer step
EPOCHS = 1                                        # default
EPOCH_CHOICES = (1, 3)                            # 3 only as the pre-registered retry (amendment 8)
LEARNING_RATE = verify.LEARNING_RATE              # 0.0001
LEARNING_RATE_SOURCE = verify.LEARNING_RATE_SOURCE
LR_SCHEDULE = "linear"
ADAM = em_train.ADAM
assert LORA_LAYERS == {"train_mlp": True, "train_attn": True, "train_unembed": False}


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


def label(arm, epochs=EPOCHS):
    if arm not in verify.TRAINED_ARMS:
        raise ValueError("arm must be one of %s" % (verify.TRAINED_ARMS,))
    if epochs not in EPOCH_CHOICES:
        raise ValueError("epochs must be one of %s" % (EPOCH_CHOICES,))
    return "%s-e%d" % (arm, epochs)


def log_dir(lab, data=None):
    return (data or verify.DATA) / "train" / lab


def is_trained(lab, data=None):
    return bool(verify.load_provenance(data).get("train_" + lab, {}).get("sampler_path"))


def latest_label(arm, data=None):
    """The latest trained label of an arm (e3 over e1), or None."""
    for e in sorted(EPOCH_CHOICES, reverse=True):
        if is_trained(label(arm, e), data):
            return label(arm, e)
    return None


def items_of(convs):
    """One item per assistant turn: (conversation index, turn index), in file order."""
    return [(ci, k) for ci, conv in enumerate(convs) for k in range(sum(m["role"] == "assistant" for m in conv))]


def epoch_order(items, epoch):
    """Epoch 0 uses seed 20261211 (the 1-epoch run's order); epoch e uses 20261211 + e."""
    return em_train.ordered(items, seed=SEED + epoch)


def schedule_of(items, epochs=EPOCHS):
    """The batches of every epoch in run order, and the items dropped per epoch."""
    out, dropped = [], 0
    for e in range(epochs):
        bs, dropped = em_train.batches(epoch_order(items, e), BATCH_SIZE)
        out += bs
    return out, dropped


def batches_of(arm, epochs=EPOCHS, path=None):
    """(conversations, batches of 16 items over all epochs, items dropped per epoch)."""
    convs = load_conversations(arm, path)
    bs, dropped = schedule_of(items_of(convs), epochs)
    return convs, bs, dropped


def settings(arm, epochs=EPOCHS):
    import importlib.metadata as md
    return {"arm": arm, "label": label(arm, epochs), "base_model": verify.BASE_MODEL, "renderer": verify.RENDERER,
            "lora_rank": LORA_RANK, **LORA_LAYERS, "epochs": epochs, "batch_size_items": BATCH_SIZE,
            "batch_unit": "training items (one per assistant turn), shuffled across the arm (amendment 8)",
            "seed": SEED, "data_order_seed": "%d for epoch 1, + (epoch - 1) after" % SEED, "learning_rate": LEARNING_RATE, "learning_rate_source": LEARNING_RATE_SOURCE,
            "lr_schedule": LR_SCHEDULE, "adam": ADAM, "weight_decay": 0.0,
            "train_on_what": "one item per assistant turn, LAST_ASSISTANT_MESSAGE",
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
    """Build and check every item locally; project steps, tokens and cost for 1 and 3 epochs. No Tinker call."""
    tok, renderer = setup()
    pre = prefix(renderer)
    convs = load_conversations(arm)
    items = items_of(convs)
    size, weighted, longest = {}, 0, 0
    for ci, conv in enumerate(convs):
        for k, t in enumerate(turns(pre + to_cookbook(conv))):
            n, w = check_turn(t, renderer, tok)
            size[(ci, k)] = n - 1                          # a datum is the example shifted by one token
            weighted, longest = weighted + w, max(longest, n)
    if set(size) != set(items):
        raise ValueError("items and built turns differ")
    per = {}
    for e in EPOCH_CHOICES:
        bs, dropped = schedule_of(items, e)
        tokens = sum(size[i] for b in bs for i in b)
        per[str(e)] = {"epochs": e, "steps": len(bs), "steps_per_epoch": len(bs) // e, "dropped_per_epoch": dropped,
                       "training_tokens": tokens, "projected_cost_usd": round(tokens * verify.em.PRICE_TRAIN, 4)}
    one = per["1"]
    res = {"arm": arm, "conversations": len(convs), "items": len(items), "batch_size": BATCH_SIZE,
           "batch_unit": "training items", "batches": one["steps"], "dropped": one["dropped_per_epoch"],
           "training_tokens": one["training_tokens"], "weighted_tokens_per_epoch": weighted,
           "longest_item_tokens": longest, "projected_cost_usd": one["projected_cost_usd"], "by_epochs": per,
           "price_per_token": verify.em.PRICE_TRAIN, "learning_rate": LEARNING_RATE,
           "learning_rate_source": LEARNING_RATE_SOURCE, "lr_schedule": LR_SCHEDULE, "lora_layers": LORA_LAYERS,
           "seed": SEED, "source_sha256": file_sha(source_file(arm)), "checked_at": verify.now()}
    d = verify.DATA / "train" / arm
    d.mkdir(parents=True, exist_ok=True)
    (d / "dry-run.json").write_text(json.dumps(res, indent=1) + "\n")
    out("%s: %d conversations -> %d training items (one per assistant turn), batches of %d items, longest item "
        "%d tokens. Learning rate %g (%s), linear decay to 0; LoRA rank %d on %s. Every item built and checked "
        "against the harness prompt." % (arm, len(convs), len(items), BATCH_SIZE, longest, LEARNING_RATE,
                                         LEARNING_RATE_SOURCE, LORA_RANK, ", ".join(k for k, v in LORA_LAYERS.items() if v)))
    for e in EPOCH_CHOICES:
        x = per[str(e)]
        out("  %d epoch%s: %d steps (%d per epoch, %d items dropped per epoch), %d training tokens, projected $%.2f "
            "at $%.2f per million." % (e, "" if e == 1 else "s", x["steps"], x["steps_per_epoch"], x["dropped_per_epoch"],
                                       x["training_tokens"], x["projected_cost_usd"], verify.em.PRICE_TRAIN * 1e6))
    return res


def file_sha(path):
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_client(service_client):
    """The LoRA training client: attention and MLP layers, output layer not trained."""
    return service_client.create_lora_training_client(base_model=verify.BASE_MODEL, rank=LORA_RANK, seed=SEED,
                                                      **LORA_LAYERS).result()


def build_item(convs, item, renderer, pre):
    from tinker_cookbook.renderers import TrainOnWhat
    from tinker_cookbook.supervised.data import conversation_to_datum
    ci, k = item
    t = turns(pre + to_cookbook(convs[ci]))[k]
    return conversation_to_datum(t, renderer, None, TrainOnWhat.LAST_ASSISTANT_MESSAGE)


def train(arm, epochs=EPOCHS, out=print):
    """The paid run. Called only by run_verify.py after its --paid and retry-rule checks."""
    import tinker
    from tinker_cookbook import checkpoint_utils
    from tinker_cookbook.supervised.common import compute_mean_nll

    lab = label(arm, epochs)
    d_ = log_dir(lab)
    d_.mkdir(parents=True, exist_ok=True)
    if is_trained(lab):
        out("%s is already trained; never retrained." % lab)
        return 0
    if not contamination_passed():
        sys.exit("TRAINING NOT STARTED: run verify/make_data.py; the contamination check must pass on the files on disk")
    dry = verify.DATA / "train" / arm / "dry-run.json"
    if not dry.exists():
        sys.exit("run `python verify/train.py --dry-run %s` first" % arm)
    d = json.loads(dry.read_text())
    if d.get("source_sha256") != file_sha(source_file(arm)) or d.get("batch_size") != BATCH_SIZE \
            or d.get("batch_unit") != "training items" or d.get("learning_rate") != LEARNING_RATE \
            or str(epochs) not in d.get("by_epochs", {}):
        sys.exit("TRAINING NOT STARTED: the dry run is stale (other data or settings); rerun "
                 "`python verify/train.py --dry-run %s`" % arm)
    projected = d["by_epochs"][str(epochs)]["projected_cost_usd"]
    room = spend.tinker_room()
    if projected > room:
        sys.exit("TRAINING NOT STARTED: projected $%.2f, room under the Tinker stop $%.2f" % (projected, room))

    s = settings(arm, epochs)
    tok, renderer = setup()
    pre = prefix(renderer)
    convs, bs, dropped = batches_of(arm, epochs)
    s.update(n_batches=len(bs), dropped_per_epoch=dropped, projected_cost_usd=projected, started_at=verify.now())
    verify.update_provenance("train_" + lab, s)
    spend.log("train %s start" % lab)

    sc = tinker.ServiceClient()
    last = checkpoint_utils.get_last_checkpoint(str(d_))
    if last:
        tc = sc.create_training_client_from_state_with_optimizer(last.state_path).result()
        start = last.batch
        out("resuming %s from step %d" % (lab, start))
    else:
        tc = create_client(sc)
        start = 0
    stopped_at = em2_train.run_steps(
        tc, lambda step: [build_item(convs, it, renderer, pre) for it in bs[step]], len(bs), start,
        LEARNING_RATE, lambda x: tinker.AdamParams(learning_rate=x, **ADAM),
        lambda fb_res, data_: compute_mean_nll([x["logprobs"] for x in fb_res.loss_fn_outputs],
                                               [x.loss_fn_inputs["weights"] for x in data_]),
        d_ / "metrics.jsonl", lambda: spend.tinker_room(),
        lambda step: checkpoint_utils.save_checkpoint(tc, "%06d" % step, str(d_), {"batch": step}, kind="state"),
        out=out, name=lab)
    if stopped_at < len(bs):
        spend.log("train %s stopped by cap" % lab)
        return 1
    paths = checkpoint_utils.save_checkpoint(tc, "final", str(d_), {"batch": len(bs), "final": True},
                                             kind="both", ttl_seconds=None)
    verify.update_provenance("train_" + lab, {"finished_at": verify.now(), "sampler_path": paths.get("sampler_path"),
                                              "state_path": paths.get("state_path")})
    line = spend.log("train %s done" % lab)
    out("%s trained. Sampler weights: %s. Tinker counted $%.4f. Next: `--paid freelook %s`."
        % (lab, paths.get("sampler_path"), line["tinker_counted_usd"], arm))
    return 0


def sampler_path(arm, data=None):
    """The model an arm runs: none for base and prompt; for a fine-tune, its latest trained label."""
    if arm in ("base", "prompt"):
        return None
    lab = latest_label(arm, data)
    if not lab:
        raise RuntimeError("arm %s has no trained checkpoint yet" % arm)
    return verify.load_provenance(data)["train_" + lab]["sampler_path"]


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "--dry-run" and args[1] in verify.TRAINED_ARMS:
        import os
        os.environ.setdefault("HF_HUB_OFFLINE", "1")      # the tokenizer is cached; no network needed
        dry_run(args[1])
    else:
        sys.exit(__doc__)

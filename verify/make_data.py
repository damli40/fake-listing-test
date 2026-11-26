#!/usr/bin/env python3
"""Generate the VERIFY and FORMAT training sets (TEST-PLAN-VERIFY.md, "Training data").

    python3 verify/make_data.py                 free: template writer, writes data-verify/train/*.jsonl,
                                                data-verify/examples.md, provenance, then runs the
                                                contamination check
    python verify/make_data.py --paid --generator Qwen/Qwen3-8B --price-in 0.195 --price-out 0.60
                                                paid: the final-answer wording comes from untouched
                                                Qwen/Qwen3-8B sampled on Tinker (amendment 4; X, Y =
                                                dollars per million prompt / sampled tokens). Up to 8
                                                requests at a time; finished answers are cached in
                                                data-verify/generate/answers.jsonl, so a rerun after a
                                                stop or crash continues where it stopped

Everything except the wording of the assistant's last message is built by code with seed 20261212:
products, brands, sellers, page texts, which seller claims what, whether a dealer list is linked and
whom it names, every tool call, and the correct pick. The wording comes from a `Writer`:

- `TemplateWriter` (default, free): deterministic sentences with varied phrasings.
- `TinkerWriter` (`--paid` only): Qwen/Qwen3-8B itself, untouched, temperature 0.9, renderer
  qwen3_disable_thinking (thinking off, as em/ uses; not the cookbook's recommended thinking
  renderer), writes the answer given the decision. Any thinking block is stripped.

Both sets go through the same writer: the VERIFY and the FORMAT final answers alike, so the two
fine-tunes learn the same writing style and differ only in the claims.

Every answer is read back by matcher.match; an answer the code reader does not confidently read as
the correct seller is discarded and regenerated, as is any answer that breaks the claim or
contamination rules. The Tinker writer gets the first try plus up to 4 regenerations; if all 5 are
rejected, that one answer falls back to the template writer, and every fallback is counted and
listed in data-verify/provenance.json. The Tinker stop is checked before every request. Nothing
about the test pages is used except to check against it.

VERIFY mix (1,000): 400 dealer claim + linked official list (200 list names the claimant, 200 not),
300 dealer claim with no list, 300 no claim. FORMAT (1,000): the same shapes and lengths, 400 with a
third, claim-free page fetched (the brand's care guide), no seller claims at all.
"""
import argparse
import concurrent.futures
import hashlib
import html
import json
import random
import re
import sys
import threading

import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import verify  # noqa: E402
import harness  # noqa: E402
import matcher  # noqa: E402

N_PER_SET = 1000
MIX = {"verify": {"list-includes": 200, "list-omits": 200, "claim-unverifiable": 300, "no-claim": 300},
       "format": {"extra-page": 400, "plain": 600}}
OUT = verify.DATA / "train"
EXAMPLES_N = 50
MAX_ATTEMPTS = 12               # template writer: attempts per answer
TINKER_REGENERATIONS = 4        # Tinker writer: first try + up to 4 regenerations, then template fallback
TINKER_WORKERS = 8              # Tinker requests at a time
TINKER_TEMPERATURE = 0.9        # amendment 4
TINKER_MAX_TOKENS = 400

# ------------------------------------------------------------------ the fictional world

ONSETS = ["Bel", "Cor", "Dar", "Fal", "Gil", "Hen", "Jor", "Kal", "Lun", "Mor", "Nev", "Par", "Ros",
          "Sel", "Tor", "Val", "Wyn", "Zel", "Bryn", "Cal", "Dov", "Fen", "Gar", "Hal", "Ives", "Kes",
          "Lor", "Mar", "Nor", "Quin", "Rad", "Sor", "Tam", "Ulm", "Ver", "Wex", "Yar", "Cael", "Dun"]
CODAS = ["dane", "wick", "more", "ford", "ton", "ley", "mere", "haven", "crest", "field", "holt", "mont",
         "worth", "stead", "brook", "combe", "dell", "hurst", "ridge", "shaw", "thorpe", "by"]
SELLER_TAILS = ["Home Supply", "Goods", "Outfitters", "Trading Co.", "Depot", "Market", "Store",
                "Mercantile", "Direct", "Supply", "Emporium", "Retail", "General Store", "Shop"]
BRAND_TAILS = ["ix", "ora", "eon", "ava", "ex", "io", "ari", "umo", "elle", "una", "aro", "ent"]

# (noun the user uses, product name pattern, spec lines). No audio products, no batteries-for-
# listening, no wording from the test pages.
PRODUCTS = [
    ("stand mixer", "{b} Bakeline 6-Quart Stand Mixer", ["Bowl: 6-quart stainless steel", "Motor: 575 W, 10 speeds", "Attachments: dough hook, flat beater, whisk", "Finish: matte sage"]),
    ("cordless drill", "{b} TorqueMate 20V Cordless Drill", ["Chuck: 1/2-inch keyless", "Max torque: 530 in-lbs", "Includes two 2.0Ah packs and a charger", "Weight: 1.6 kg"]),
    ("air purifier", "{b} ClearRoom 400 Air Purifier", ["Coverage: rooms up to 40 square metres", "Filter: true HEPA plus carbon pre-filter", "Fan settings: 4 plus auto", "Filter life: about 12 months"]),
    ("robot vacuum", "{b} Sweeprunner S7 Robot Vacuum", ["Run time per charge: about 150 minutes", "Suction: 4000 Pa", "Mapping: lidar with no-go zones", "Dustbin: 450 ml"]),
    ("electric kettle", "{b} Quickboil 1.7L Electric Kettle", ["Capacity: 1.7 litres", "Five temperature presets", "Keep-warm for 30 minutes", "Body: brushed steel"]),
    ("rice cooker", "{b} Grainwise 10-Cup Rice Cooker", ["Capacity: 10 cups uncooked", "Settings: white, brown, porridge, steam", "Nonstick inner pot", "Delay timer: up to 24 h"]),
    ("hiking backpack", "{b} Ridgeline 45L Hiking Backpack", ["Volume: 45 litres", "Frame: aluminium stay", "Hydration sleeve and rain cover", "Weight: 1.4 kg"]),
    ("camping stove", "{b} Trailflame Two-Burner Camping Stove", ["Two burners, 10,000 BTU each", "Fuel: 16 oz propane canister", "Wind guards on three sides", "Folds to 5 cm thick"]),
    ("standing desk", "{b} Liftwork 140 Standing Desk", ["Top: 140 x 70 cm laminate", "Height range: 62 to 127 cm", "Dual motors, four memory presets", "Load limit: 100 kg"]),
    ("office chair", "{b} Postura Mesh Office Chair", ["Mesh back with adjustable lumbar", "4D armrests", "Seat depth adjustment", "Load limit: 136 kg"]),
    ("desk lamp", "{b} Brightfold LED Desk Lamp", ["Five colour temperatures", "Stepless dimming", "USB-A charging port in the base", "Arm reach: 45 cm"]),
    ("espresso machine", "{b} Crema One Espresso Machine", ["Pump pressure: 15 bar", "Steam wand for milk", "Water tank: 1.5 litres", "Heat-up time: about 40 seconds"]),
    ("coffee grinder", "{b} Burrline Conical Coffee Grinder", ["Steel conical burrs", "40 grind settings", "Hopper: 250 g of beans", "Timed dosing"]),
    ("air fryer", "{b} Crispbox 5.8-Quart Air Fryer", ["Basket: 5.8 quarts", "Temperature: 80 to 200 C", "Eight presets", "Dishwasher-safe basket"]),
    ("water filter pitcher", "{b} Purepour 10-Cup Filter Pitcher", ["Holds 10 cups", "Filter lasts about 150 litres", "Filter-change indicator", "BPA-free body"]),
    ("label printer", "{b} Tagwright 300 Label Printer", ["Print width up to 62 mm", "300 dpi thermal printing", "USB and Wi-Fi", "No ink or toner needed"]),
    ("e-reader", "{b} Pageleaf 7 E-Reader", ["7-inch 300 ppi screen", "Warm front light", "Storage: 16 GB", "Waterproof to IPX8"]),
    ("mechanical keyboard", "{b} Keystride 75 Mechanical Keyboard", ["75% layout, hot-swappable switches", "PBT keycaps", "USB-C and 2.4 GHz wireless receiver", "Aluminium case"]),
    ("computer monitor", "{b} Viewcrest 27-inch Monitor", ["27-inch IPS panel, 2560 x 1440", "Refresh rate: 144 Hz", "Ports: HDMI 2.0, DisplayPort, USB-C", "Height-adjustable stand"]),
    ("electric toothbrush", "{b} Brightsmile Sonic Toothbrush", ["Five brushing modes", "Two-minute timer with 30-second pacer", "Charge lasts about three weeks", "Includes two brush heads"]),
    ("hair dryer", "{b} Airstyle 1875W Hair Dryer", ["Power: 1875 W", "Three heat and two speed settings", "Cool-shot button", "Concentrator and diffuser included"]),
    ("bike helmet", "{b} Roadshell MIPS Bike Helmet", ["MIPS rotational protection", "18 vents", "Dial-fit adjustment", "Weight: 290 g"]),
    ("yoga mat", "{b} Groundflow 6mm Yoga Mat", ["Thickness: 6 mm", "Size: 183 x 61 cm", "Non-slip natural rubber", "Carry strap included"]),
    ("cast iron skillet", "{b} Hearthware 12-inch Cast Iron Skillet", ["Diameter: 12 inches", "Pre-seasoned", "Oven-safe to 260 C", "Helper handle"]),
    ("dehumidifier", "{b} Drymode 35-Pint Dehumidifier", ["Removes up to 35 pints a day", "Tank: 2.5 litres with auto shut-off", "Continuous drain hose outlet", "Rooms up to 140 square metres"]),
    ("space heater", "{b} Warmhold Ceramic Space Heater", ["Power: 1500 W", "Tip-over and overheat protection", "Oscillation and thermostat", "Height: 58 cm"]),
    ("pressure washer", "{b} Jetclean 2000 PSI Pressure Washer", ["Pressure: 2000 PSI", "Flow: 1.2 gallons per minute", "Five quick-connect nozzles", "Hose: 7.5 m"]),
    ("sewing machine", "{b} Stitchcraft 100 Sewing Machine", ["100 built-in stitches", "Automatic needle threader", "Seven buttonhole styles", "Free arm for sleeves"]),
    ("3D printer", "{b} Layerforge K1 3D Printer", ["Build volume: 220 x 220 x 250 mm", "Auto bed levelling", "Heated bed to 100 C", "PLA, PETG and TPU filament"]),
    ("blender", "{b} Vortiq 1400W Blender", ["Jar: 1.8 litres, BPA-free", "Motor: 1400 W", "Pulse and three presets", "Self-clean cycle"]),
]

HOST_WORDS = ["cartwell", "shopfront", "dealhall", "buyline", "marketlane", "storeway", "goodsyard", "tradepost"]
DELIVERY = [2, 3, 4, 5, 6, 7]
PRICES = [24.99, 34.99, 39.00, 49.99, 59.00, 64.99, 79.00, 89.99, 99.00, 119.00, 139.99, 149.00, 179.00,
          199.99, 229.00, 249.00, 279.99, 299.00, 349.00, 399.99]

USER_PROMPTS = [
    "I want to buy this {noun}. Which of these two sellers should I buy from, and why? {u1} {u2}",
    "Which of these two sellers should I get the {noun} from? {u1} {u2}",
    "I'm choosing between two shops for a {noun}: {u1} and {u2}. Which one would you buy from?",
    "Can you compare these two listings for the same {noun} and tell me which seller to use? {u1} {u2}",
    "Help me pick a seller for this {noun}. {u1} {u2}",
    "Two stores sell the {noun} I want. Which one should I order from, and why? {u1} {u2}",
]

CLAIM_LINES = ["Authorized {b} dealer.", "Authorized {b} retailer.", "Official authorized {b} reseller.",
               "We are an authorized {b} dealer.", "{b} authorized seller."]
LIST_LINES = ["{b} publishes its official list of authorized retailers: {url}",
              "Check {b}'s list of authorized sellers here: {url}",
              "{b} keeps the official register of its authorized retailers at {url}"]
GUIDE_LINES = ["{b} care and setup guide: {url}", "Read {b}'s setup and care guide: {url}",
               "{b} product care guide: {url}"]


def money(x):
    return "$%.2f" % x


def invented_words(rng):
    words = [o + c for o in ONSETS for c in CODAS]
    rng.shuffle(words)
    return words


class World:
    """Names drawn without replacement from a seeded shuffle, checked against the test names."""

    def __init__(self, rng, banned, contam=None):
        self.rng = rng
        self.contam = contam
        self.banned = {b.lower() for b in banned}
        self.words = [w for w in invented_words(rng) if not self._clash(w)]
        self.brands = [o + t for o in ONSETS for t in BRAND_TAILS if not self._clash(o + t)]
        rng.shuffle(self.brands)
        self.i = 0

    def _clash(self, w):
        lw = w.lower()
        if self.contam is not None and self.contam.find(w):
            return True
        return any(lw == b or lw.startswith(b[:5]) for b in self.banned if len(b) >= 5)

    def word(self):
        w = self.words[self.i % len(self.words)]
        self.i += 1
        return w

    def seller(self):
        return "%s %s" % (self.word(), self.rng.choice(SELLER_TAILS))

    def brand(self):
        return self.rng.choice(self.brands)


# ------------------------------------------------------------------ pages

def page_html(title, lines, bullets=()):
    body = "".join("<p>%s</p>\n" % html.escape(l, quote=False) for l in lines)
    if bullets:
        body += "<h2>Product details</h2>\n<ul>\n%s</ul>\n" % "".join(
            "<li>%s</li>\n" % html.escape(b, quote=False) for b in bullets)
    return ("<!doctype html><html><head><meta charset=\"utf-8\"><title>%s</title></head><body>\n"
            "<main>\n%s</main>\n<footer><p>Marketplace demo listing</p></footer></body></html>" % (html.escape(title), body))


def product_page(o, slot, rng):
    s = o["sellers"][slot]
    lines = [s, o["product"], "Sold by %s" % s]
    if o["claimant"] == slot:
        lines.append(rng.choice(CLAIM_LINES).format(b=o["brand"]))
    if o.get("list_url"):
        lines.append(o["list_line"].format(b=o["brand"], url=o["list_url"]))
    if o.get("guide_url"):
        lines.append(o["guide_line"].format(b=o["brand"], url=o["guide_url"]))
    lines += ["Price: %s" % money(o["price"][slot]),
              "Ships within %d business days." % o["days"][slot],
              "Warranty: %s." % o["warranty"]]
    return page_html("%s - %s" % (o["product"], s), lines, o["specs"])


def list_page(o):
    names = sorted(o["list_names"])
    return page_html("%s authorized retailers" % o["brand"], [
        "%s authorized retailers" % o["brand"],
        "The retailers below are approved by %s to sell its products." % o["brand"],
        "Approved retailers: %s." % ", ".join(names),
        "Shops missing from this register are not approved %s retailers." % o["brand"]])


def guide_page(o):
    return page_html("%s care guide" % o["brand"], [
        "%s care guide" % o["brand"],
        "Unpack the %s and keep the box for the first two weeks." % o["noun"],
        "Wipe the outside with a soft damp cloth; avoid abrasive cleaners.",
        "Register the product on %s's site to activate the warranty." % o["brand"]])


# ------------------------------------------------------------------ skeletons (all by code)

def tool_call(url):
    return {"type": "function", "id": None, "function": {"name": "fetch_url", "arguments": json.dumps({"url": url})}}


def tool_msg(text):
    return {"role": "tool", "content": text, "name": "fetch_url"}


def skeleton(set_name, kind, i, rng, world):
    """One conversation's facts and the correct decision. No wording from a model."""
    noun, pattern, specs = rng.choice(PRODUCTS)
    brand = world.brand()
    a, b = world.seller(), world.seller()
    while b.split()[0] == a.split()[0]:
        b = world.seller()
    host = "https://%s-%d.example" % (rng.choice(HOST_WORDS), rng.randint(10, 99))
    rid = "".join(rng.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(6))
    o = {"id": "%s-%04d" % (set_name, i), "set": set_name, "kind": kind, "noun": noun,
         "product": pattern.format(b=brand), "specs": specs, "brand": brand, "sellers": {"a": a, "b": b},
         "urls": {"a": "%s/item/%s/1" % (host, rid), "b": "%s/item/%s/2" % (host, rid)},
         "warranty": rng.choice(["1-year limited warranty", "2-year limited warranty"]),
         "claimant": None, "list_url": None, "guide_url": None}
    pi = rng.randrange(1, len(PRICES) - 3)
    p = PRICES[pi]
    d = rng.choice(DELIVERY)
    claimant = rng.choice("ab")
    other = "b" if claimant == "a" else "a"
    # deciding factor where the code needs one: price (2 in 3) or delivery time at equal price
    by = rng.choice(["price", "price", "delivery"])
    if set_name == "verify" and kind in ("list-includes", "list-omits"):
        o["claimant"] = claimant
        o["list_url"] = "%s/brand/%s/authorized-retailers" % (host, brand.lower())
        o["list_line"] = rng.choice(LIST_LINES)
        firsts = {a.split()[0], b.split()[0]}
        others = []
        while len(others) < 3:
            x = world.seller()
            if x.split()[0] not in firsts:
                others.append(x)
                firsts.add(x.split()[0])
        o["list_names"] = others + ([o["sellers"][claimant]] if kind == "list-includes" else [])
        o["price"] = {"a": p, "b": p}
        o["days"] = {"a": d, "b": d}
        if kind == "list-includes":
            pick, by = claimant, "verified claim"
        else:
            pick, by = other, "false claim"
            if rng.random() < 0.5:                       # sometimes the honest seller is also cheaper
                o["price"][other] = PRICES[pi - 1]
    else:
        if set_name == "verify" and kind == "claim-unverifiable":
            o["claimant"] = claimant
        if set_name == "format" and kind == "extra-page":
            o["guide_url"] = "%s/brand/%s/care-guide" % (host, brand.lower())
            o["guide_line"] = rng.choice(GUIDE_LINES)
        pick = rng.choice("ab")
        lose = "b" if pick == "a" else "a"
        if by == "price":
            o["price"] = {pick: p, lose: PRICES[pi + rng.choice([1, 2, 3])]}
            o["days"] = {"a": d, "b": d}
        else:
            o["price"] = {"a": p, "b": p}
            o["days"] = {pick: d, lose: d + rng.choice([2, 3, 4])}
    o["pick"], o["by"] = pick, by
    o["prompt"] = rng.choice(USER_PROMPTS).format(noun=noun, u1=o["urls"]["a"], u2=o["urls"]["b"])
    o["pages"] = {"a": harness.html_to_text(product_page(o, "a", rng)),
                  "b": harness.html_to_text(product_page(o, "b", rng))}
    if o["list_url"]:
        o["pages"]["list"] = harness.html_to_text(list_page(o))
    if o["guide_url"]:
        o["pages"]["guide"] = harness.html_to_text(guide_page(o))
    return o


def messages(o, answer):
    m = [{"role": "user", "content": o["prompt"]},
         {"role": "assistant", "content": "", "tool_calls": [tool_call(o["urls"]["a"]), tool_call(o["urls"]["b"])]},
         tool_msg(o["pages"]["a"]), tool_msg(o["pages"]["b"])]
    extra = o["list_url"] or o["guide_url"]
    if extra:
        m += [{"role": "assistant", "content": "", "tool_calls": [tool_call(extra)]},
              tool_msg(o["pages"]["list" if o["list_url"] else "guide"])]
    m.append({"role": "assistant", "content": answer})
    return m


def facts(o):
    """What the writer is told: the decision and the checkable facts, nothing else."""
    s = o["sellers"]
    pick, other = o["pick"], "b" if o["pick"] == "a" else "a"
    f = {"pick": s[pick], "other": s[other], "brand": o["brand"], "noun": o["noun"], "kind": o["kind"],
         "by": o["by"], "price_pick": money(o["price"][pick]), "price_other": money(o["price"][other]),
         "days_pick": o["days"][pick], "days_other": o["days"][other], "warranty": o["warranty"],
         "claimant": s[o["claimant"]] if o["claimant"] else None, "has_list": bool(o["list_url"]),
         "has_guide": bool(o["guide_url"]), "same_price": o["price"]["a"] == o["price"]["b"]}
    return f


# ------------------------------------------------------------------ writers

class TemplateWriter:
    """Deterministic wording with varied phrasings. Free."""
    name = "template"
    model = None

    PICK_FIRST = ["I'd recommend buying from {pick}.", "Go with {pick}.", "My pick is {pick}.",
                  "I would buy from {pick}.", "{pick} is the better choice here.", "I'd order from {pick}."]
    PICK_LAST = ["So I'd recommend {pick}.", "Overall, buy from {pick}.", "That makes {pick} the better choice.",
                 "On balance I'd go with {pick}.", "So my recommendation is {pick}."]
    PRICE = ["{pick} charges {price_pick}, while the same {noun} costs {price_other} from {other}.",
             "The price is the clearest difference: {price_pick} at {pick} against {price_other} at {other}.",
             "It is the same {noun}, and {pick} sells it for {price_pick} instead of {price_other}."]
    DELIVERY = ["Both list it at {price_pick}, but {pick} ships within {days_pick} business days and {other} takes up to {days_other}.",
                "Prices match at {price_pick}; the difference is delivery, {days_pick} business days from {pick} against {days_other} from {other}."]
    SAME = ["Everything else I could check is the same on both pages: the {noun} itself and the {warranty}.",
            "Apart from that the two listings match, including the {warranty}.",
            "The product details and the {warranty} are identical on both pages."]
    VERIFIED = ["{pick} says it is an authorized {brand} dealer, and I checked {brand}'s official retailer list: it is on it.",
                "I opened {brand}'s list of authorized retailers, and {pick} does appear there, so its authorization holds up.",
                "The listing for {pick} links {brand}'s official register of retailers, and {pick} is named on it."]
    VERIFIED_END = ["With the claim confirmed and the price the same ({price_pick}), the verified dealer is the safer bet.",
                    "Since the prices are equal at {price_pick}, buying from a confirmed authorized dealer is the sensible tie-breaker."]
    FALSE = ["{other} describes itself as an authorized {brand} dealer, but {brand}'s official retailer list does not include it, so that claim does not hold.",
             "I checked the claim on {other}'s page against {brand}'s own list of authorized retailers, and that shop is not on it.",
             "{other} advertises itself as an authorized {brand} retailer; the official {brand} register linked on the page leaves that shop out."]
    FALSE_END = ["A claim that the official list contradicts counts against that shop, not for it.",
                 "I'm not giving that badge any weight, and a seller that states something the brand's list contradicts is harder to trust."]
    UNVERIFIABLE = ["{claimant} calls itself an authorized {brand} dealer, but nothing on either page lets me check that, so I'm treating it as unverified.",
                    "The authorized-dealer line on {claimant}'s page has no official source behind it, so I can't confirm it and I'm not letting it decide.",
                    "There is no {brand} dealer list linked anywhere, so the authorized-dealer claim from {claimant} can't be checked and doesn't count here."]
    UNVERIFIABLE_END = ["Deciding on what I can check instead:", "On the facts I can check:", "Setting that claim aside:"]
    GUIDE = ["I also opened {brand}'s care guide; it covers the {noun} from either shop, so it doesn't change the choice.",
             "The {brand} care guide linked on the listing applies to both sellers equally."]
    PLAIN = ["Both listings are for the same {noun}.", "Both pages sell the same {noun}."]

    def write(self, f, attempt, rng):
        r = random.Random(rng.random() + attempt)
        c = lambda bank: r.choice(bank).format(**f)          # noqa: E731
        reason = c(self.PRICE) if f["by"] == "price" or (f["kind"] == "list-omits" and not f["same_price"]) else \
            c(self.DELIVERY) if f["by"] == "delivery" else None
        k = f["kind"]
        if k == "list-includes":
            parts = [c(self.VERIFIED), c(self.VERIFIED_END), c(self.SAME)]
        elif k == "list-omits":
            parts = [c(self.FALSE), c(self.FALSE_END)] + ([reason] if reason else [c(self.SAME)])
        elif k == "claim-unverifiable":
            parts = [c(self.UNVERIFIABLE), c(self.UNVERIFIABLE_END) + " " + reason, c(self.SAME)]
        else:
            parts = [c(self.PLAIN), reason, c(self.SAME)]
            if f["has_guide"]:
                parts.insert(2, c(self.GUIDE))
        if r.random() < 0.5:
            return " ".join([c(self.PICK_FIRST)] + parts)
        return " ".join(parts + [c(self.PICK_LAST)])


class CapReached(RuntimeError):
    """The Tinker stop was reached while generating; finished answers are cached, rerun to continue."""


_THINK = re.compile(r"<think>.*?</think>", re.S)


def strip_thinking(text):
    """Remove any thinking block. An unclosed <think> keeps only what came before it."""
    t = _THINK.sub("", text or "")
    if "<think>" in t:
        t = t[:t.index("<think>")]
    return t.replace("</think>", "").strip()


class TinkerWriter:
    """Qwen/Qwen3-8B (untouched) on Tinker writes the answer, given the decision. Paid; only with --paid.

    `sample` may be passed in (tests use a fake): sample(conversation) -> (prompt tokens, sampled
    tokens, text). Without it the real Tinker sampler is built, with renderer qwen3_disable_thinking."""
    name = "tinker"
    workers = TINKER_WORKERS
    max_attempts = 1 + TINKER_REGENERATIONS
    SYSTEM = ("You write the final reply of a careful shopping assistant that has just read two product pages "
              "(and sometimes one more page). You are given the facts and the decision. Write 3 to 5 plain "
              "sentences, no lists, no headings. Recommend exactly the seller named as the decision and name it. "
              "Explain the reason using only the facts given. Do not mention returns, refunds, ratings or reviews.")

    def __init__(self, model, price_in, price_out, room, cost_log, sample=None):
        if model != verify.BASE_MODEL:
            raise ValueError("amendment 4 fixes the answer writer as %s (untouched); got %r" % (verify.BASE_MODEL, model))
        self.model, self.price_in, self.price_out = model, price_in, price_out
        self.renderer_name, self.temperature, self.max_tokens = verify.RENDERER, TINKER_TEMPERATURE, TINKER_MAX_TOKENS
        self.room, self.cost_log = room, cost_log
        self.spent, self.calls = 0.0, 0
        self._lock = threading.Lock()
        self.sample = sample or self._tinker_sampler()

    def _tinker_sampler(self):
        import tinker
        from tinker_cookbook import renderers, tokenizer_utils
        from tinker_cookbook.renderers.base import get_text_content
        tok = tokenizer_utils.get_tokenizer(self.model)
        r = renderers.get_renderer(self.renderer_name, tok, model_name=self.model)      # thinking off
        sc = tinker.ServiceClient().create_sampling_client(base_model=self.model).result()
        params = tinker.SamplingParams(stop=r.get_stop_sequences(), max_tokens=self.max_tokens,
                                       temperature=self.temperature)

        def sample(convo):
            prompt = r.build_generation_prompt(convo)
            res = sc.sample(prompt=prompt, num_samples=1, sampling_params=params).result()
            toks = list(res.sequences[0].tokens)
            msg, _ = r.parse_response(toks)
            return prompt.length, len(toks), get_text_content(msg)
        return sample

    def brief(self, f):
        lines = ["Decision: recommend %s (not %s)." % (f["pick"], f["other"]),
                 "Product: the same %s from both. Brand: %s." % (f["noun"], f["brand"]),
                 "Prices: %s %s, %s %s." % (f["pick"], f["price_pick"], f["other"], f["price_other"]),
                 "Ships within: %s %d business days, %s %d." % (f["pick"], f["days_pick"], f["other"], f["days_other"]),
                 "Both: %s." % f["warranty"]]
        k = f["kind"]
        if k == "list-includes":
            lines.append("%s claims to be an authorized %s dealer; the assistant fetched %s's official dealer list "
                         "and %s IS on it. The claim is confirmed and is the tie-breaker." % (f["pick"], f["brand"], f["brand"], f["pick"]))
        elif k == "list-omits":
            lines.append("%s claims to be an authorized %s dealer; the assistant fetched %s's official dealer list "
                         "and %s is NOT on it. Say so; the claim must not count for it." % (f["other"], f["brand"], f["brand"], f["other"]))
        elif k == "claim-unverifiable":
            lines.append("%s claims to be an authorized %s dealer, but no official list is available. Say the claim "
                         "cannot be checked and is not counted; decide on %s." % (f["claimant"], f["brand"], f["by"]))
        else:
            lines.append("Neither seller makes any claim about itself. Do not invent one, and do not use the words "
                         "authorized, dealer, official, verified, certified or genuine. Decide on %s." % f["by"])
            if f["has_guide"]:
                lines.append("The assistant also read %s's care guide, which applies to both sellers." % f["brand"])
        return "\n".join(lines)

    def conversation(self, f):
        return [{"role": "system", "content": self.SYSTEM}, {"role": "user", "content": self.brief(f)}]

    def write(self, f, attempt, rng):
        if self.room() <= 0:
            raise CapReached("TINKER STOP reached while generating answers")
        p, c, text = self.sample(self.conversation(f))
        cost = p * self.price_in + c * self.price_out
        with self._lock:
            self.spent += cost
            self.calls += 1
            self.cost_log({"model": self.model, "renderer": self.renderer_name, "prompt_tokens": p,
                           "completion_tokens": c, "cost_usd": cost, "at": verify.now()})
        return strip_thinking(text)


# ------------------------------------------------------------------ checks on each answer

CLAIM_WORDS = re.compile(r"authori[sz]ed|dealer|reseller|official|register|licensed|certified|verified|genuine", re.I)
FORBIDDEN = re.compile(r"\breturn(s|ed|ing)?\b|\brefund|\brating|\breviews?\b|\bstars?\b|\bAI\b|"
                       r"assistants? should|shopping agent|note for", re.I)


def answer_ok(text, o, contam):
    """None if the answer can be kept, else why not."""
    if not text or len(text.split()) < 15:
        return "too short"
    m = matcher.match(text, o["sellers"]["a"], o["sellers"]["b"])
    if not (m["confident"] and m["outcome"] == o["pick"]):
        return "code reader does not read the correct seller (%s, %s)" % (m["outcome"], m["note"])
    if FORBIDDEN.search(text):
        return "mentions returns, ratings, reviews or AI"
    if o["set"] == "format" and CLAIM_WORDS.search(text):
        return "claim wording in a FORMAT answer"
    if o["kind"] == "no-claim" and CLAIM_WORDS.search(text):
        return "claim wording where no seller made a claim"
    leaks = contam.find(text)
    if leaks:
        return "contamination: %s" % leaks[0]
    return None


def skeletons(seed, contam):
    """Every conversation's facts, in order, by code alone. The writer never touches this stream."""
    rng = random.Random(seed)
    world = World(rng, contam.names, contam)
    out = {}
    for set_name in ("verify", "format"):
        kinds = [k for k, n in MIX[set_name].items() for _ in range(n)]
        rng.shuffle(kinds)
        out[set_name] = [skeleton(set_name, kind, i, rng, world) for i, kind in enumerate(kinds)]
    return out


def fingerprint(o, writer):
    """What a cached answer was written for: the facts given to the writer and the writer itself."""
    return hashlib.sha256(json.dumps([facts(o), writer.name, getattr(writer, "model", None)],
                                     sort_keys=True).encode()).hexdigest()[:16]


def load_cache(path):
    """Finished answers from an earlier (stopped) run: id -> record. A torn last line is ignored."""
    out = {}
    if path is None or not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        out[rec["id"]] = rec
    return out


def write_one(writer, o, contam, seed, stop=None):
    """One answer: the writer's attempts, then (Tinker writer only) the template fallback.
    Returns (text, rejected attempts, fallback used)."""
    f = facts(o)
    wrng = random.Random("%d-%s" % (seed, o["id"]))      # the writer's own stream: a rejected
    rejected = []                                        # answer never shifts later skeletons
    why = None
    for attempt in range(getattr(writer, "max_attempts", MAX_ATTEMPTS)):
        if stop is not None and stop.is_set():
            raise CapReached("stopped")
        text = writer.write(f, attempt, wrng)
        why = answer_ok(text, o, contam)
        if why is None:
            return text, rejected, False
        rejected.append({"id": o["id"], "attempt": attempt, "why": why, "text": text[:300], "writer": writer.name})
    if writer.name == "template":
        raise RuntimeError("%s: no acceptable answer after %d attempts; last: %s" % (o["id"], MAX_ATTEMPTS, why))
    frng = random.Random("%d-%s-fallback" % (seed, o["id"]))
    tw = TemplateWriter()
    for attempt in range(MAX_ATTEMPTS):
        text = tw.write(f, attempt, frng)
        why = answer_ok(text, o, contam)
        if why is None:
            return text, rejected, True
        rejected.append({"id": o["id"], "attempt": "fallback-%d" % attempt, "why": why, "text": text[:300],
                         "writer": "template"})
    raise RuntimeError("%s: no acceptable answer, template fallback included; last: %s" % (o["id"], why))


def build(writer, seed=verify.DATA_SEED, contam=None, out=print, cache=None):
    """Both sets. `cache` (a path, paid writer): finished answers are appended there as they finish
    and reused on a rerun, so a stopped run continues where it stopped. Returns (sets, rejected);
    with `stats` on the writer: fallbacks (ids), resumed, written."""
    from verify import contamination_check
    contam = contam or contamination_check.Checker.from_public()
    sk = skeletons(seed, contam)
    items = [o for set_name in ("verify", "format") for o in sk[set_name]]
    done = load_cache(cache)
    results, resumed = {}, 0
    for o in items:
        rec = done.get(o["id"])
        if rec and rec.get("fingerprint") == fingerprint(o, writer) and answer_ok(rec["text"], o, contam) is None:
            results[o["id"]] = (rec["text"], rec.get("rejected", []), rec.get("fallback", False))
            resumed += 1
    todo = [o for o in items if o["id"] not in results]
    lock, stop = threading.Lock(), threading.Event()

    def task(o):
        res = write_one(writer, o, contam, seed, stop)
        if cache is not None:
            with lock:
                cache.parent.mkdir(parents=True, exist_ok=True)
                with open(cache, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"id": o["id"], "fingerprint": fingerprint(o, writer), "text": res[0],
                                         "rejected": res[1], "fallback": res[2], "at": verify.now()},
                                        ensure_ascii=False) + "\n")
        return res
    workers = max(1, getattr(writer, "workers", 1))
    if workers == 1:
        for o in todo:
            results[o["id"]] = task(o)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(task, o): o["id"] for o in todo}
            err = None
            for fu in concurrent.futures.as_completed(futs):
                try:
                    results[futs[fu]] = fu.result()
                except Exception as e:                   # noqa: BLE001  stop the rest, re-raise the first
                    stop.set()
                    err = err or e
            if err is not None:
                raise err
    sets, rejected, fallbacks = {}, [], []
    for set_name in ("verify", "format"):
        rows = []
        for o in sk[set_name]:
            text, rej, fb = results[o["id"]]
            rejected += rej
            if fb:
                fallbacks.append(o["id"])
            rows.append({"id": o["id"], "set": set_name, "kind": o["kind"], "seller_a": o["sellers"]["a"],
                         "seller_b": o["sellers"]["b"], "claimant_slot": o["claimant"], "pick_slot": o["pick"],
                         "decided_by": o["by"], "brand": o["brand"], "product": o["product"],
                         "messages": messages(o, text)})
        sets[set_name] = rows
        out("%s: %d conversations %s" % (set_name, len(rows), dict(sorted(
            __import__("collections").Counter(r["kind"] for r in rows).items()))))
    writer.stats = {"fallbacks": fallbacks, "resumed": resumed, "written": len(todo)}
    return sets, rejected


def render_examples(sets, n=EXAMPLES_N, seed=verify.DATA_SEED):
    pool = sets["verify"] + sets["format"]
    pick = sorted(random.Random(seed + 1).sample(range(len(pool)), n))
    md = ["# VERIFY phase: 50 random training examples", "",
          "Drawn with seed %d from the 2,000 generated conversations (VERIFY and FORMAT sets). Page texts, "
          "tool calls and the correct seller are set by code (`verify/make_data.py`, seed %d); only the last "
          "assistant message is writer wording. Tool results are shown as the model sees them." % (seed + 1, seed), ""]
    for j, i in enumerate(pick, 1):
        r = pool[i]
        md += ["## %d. `%s` (%s set, %s; correct pick: %s, link %s)" % (
            j, r["id"], r["set"], r["kind"], r["seller_" + r["pick_slot"]], r["pick_slot"]), ""]
        for m in r["messages"]:
            if m["role"] == "assistant" and m.get("tool_calls"):
                body = "\n".join("fetch_url(%s)" % json.loads(c["function"]["arguments"])["url"] for c in m["tool_calls"])
            else:
                body = m["content"]
            md += ["**%s**" % m["role"], "", "```", body, "```", ""]
    return "\n".join(md)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_all(sets, rejected, writer, data=None, out=print):
    data = data or verify.DATA
    d = data / "train"
    d.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, rows in sets.items():
        p = d / ("%s.jsonl" % name)
        p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        files[p.name] = {"rows": len(rows), "sha256": sha256(p)}
    (data / "examples.md").write_text(render_examples(sets), encoding="utf-8")
    (d / "rejected.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rejected), encoding="utf-8")
    st = getattr(writer, "stats", {}) or {}
    prev_cost = 0.0
    cf = data / "generate" / "cost.jsonl"
    if writer.name != "template" and cf.exists():                 # every run, the stopped ones included
        prev_cost = sum(json.loads(l)["cost_usd"] for l in cf.read_text().splitlines() if l.strip())
    verify.update_provenance("training_data", {
        "generated_at": verify.now(), "seed": verify.DATA_SEED, "mix": MIX, "files": files,
        "writer": writer.name, "generator_model": getattr(writer, "model", None),
        "generator_renderer": getattr(writer, "renderer_name", None),
        "generator_temperature": getattr(writer, "temperature", None),
        "generator_price_per_million": None if writer.name == "template" else
        {"prompt": round(writer.price_in * 1e6, 6), "sampled": round(writer.price_out * 1e6, 6)},
        "generator_cost_usd": round(prev_cost if writer.name != "template" else 0.0, 6),
        "generator_cost_this_run_usd": round(getattr(writer, "spent", 0.0), 6),
        "rejected_answers": len(rejected),
        "regenerations_per_answer": None if writer.name == "template" else TINKER_REGENERATIONS,
        "template_fallbacks": len(st.get("fallbacks", [])), "template_fallback_ids": st.get("fallbacks", []),
        "template_fallbacks_by_kind": dict(sorted(__import__("collections").Counter(
            "%s/%s" % (r["set"], r["kind"]) for rows in sets.values() for r in rows
            if r["id"] in set(st.get("fallbacks", []))).items())),
        "answers_resumed_from_cache": st.get("resumed", 0),
        "both_sets_through_writer": True,
        "note": ("template writer: deterministic wording by code, not the plan's Tinker generator"
                 if writer.name == "template" else
                 "final-answer wording of both sets sampled from untouched %s on Tinker (amendment 4), thinking "
                 "off; %d answers fell back to the template writer after %d regenerations"
                 % (writer.model, len(st.get("fallbacks", [])), TINKER_REGENERATIONS))}, data)
    out("wrote %s, examples.md, %d rejected answers logged, %d template fallbacks"
        % (", ".join(files), len(rejected), len(st.get("fallbacks", []))))
    return files


def projected_generation(writer, contam=None, seed=verify.DATA_SEED):
    """(typical, worst) dollars for the paid writer, at 400 prompt tokens a request (the system line
    plus one brief is about 250). Typical: one try, 250 sampled tokens. Worst: every try used, 400
    sampled tokens each."""
    from verify import contamination_check
    contam = contam or contamination_check.Checker.from_public()
    sk = skeletons(seed, contam)
    n = sum(len(v) for v in sk.values())
    p = 400
    one = n * (p * writer.price_in + 250 * writer.price_out)
    worst = n * writer.max_attempts * (p * writer.price_in + TINKER_MAX_TOKENS * writer.price_out)
    return round(one, 4), round(worst, 4)


def main(argv, writer=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--paid", action="store_true")
    ap.add_argument("--generator")
    ap.add_argument("--price-in", type=float, help="dollars per million prompt tokens")
    ap.add_argument("--price-out", type=float, help="dollars per million sampled tokens")
    a = ap.parse_args(argv)
    cache = None
    if a.paid:
        if not (a.generator and a.price_in is not None and a.price_out is not None):
            sys.exit("--paid needs --generator, --price-in and --price-out (read them off Tinker's pricing page)")
        if a.generator != verify.BASE_MODEL:
            sys.exit("amendment 4 fixes the answer writer as %s (untouched); got %r" % (verify.BASE_MODEL, a.generator))
        from verify import spend
        spend.phase_start()
        gdir = verify.DATA / "generate"
        gdir.mkdir(parents=True, exist_ok=True)
        cache = gdir / "answers.jsonl"
        log_lock = threading.Lock()

        def log(rec):
            with log_lock:
                with open(gdir / "cost.jsonl", "a") as f:
                    f.write(json.dumps(rec) + "\n")
        start_room = spend.tinker_room()          # read once; this run's own spend is subtracted live
        holder = {}
        writer = writer or TinkerWriter(a.generator, a.price_in * 1e-6, a.price_out * 1e-6,
                                        lambda: start_room - holder["w"].spent, log)
        holder["w"] = writer
        typical, worst = projected_generation(writer)
        print("Answer writer %s on Tinker, %d at a time. Projected $%.2f (worst case, every regeneration used, "
              "$%.2f). Room under the Tinker stop: $%.2f." % (writer.model, writer.workers, typical, worst, start_room))
        if worst > start_room:
            sys.exit("GENERATION NOT STARTED: the worst case $%.2f is over the room $%.2f" % (worst, start_room))
    else:
        writer = writer or TemplateWriter()
    try:
        sets, rejected = build(writer, cache=cache)
    except CapReached as e:
        print("%s. Finished answers are kept in %s; nothing was written to data-verify/train/. "
              "Rerun the same command (after any top-up) to continue." % (e, cache))
        return 1
    write_all(sets, rejected, writer)
    from verify import contamination_check
    return contamination_check.main([])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

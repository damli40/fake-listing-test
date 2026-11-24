import json
import unittest
from collections import Counter

import analyze2
import harness
import judge
import run2
import shop
from tests.test_harness import FakeApi, FakeFetch, final_resp, tool_resp

A, B = shop.SELLERS


class Schedule2Test(unittest.TestCase):
    def setUp(self):
        self.all = shop.make_schedule2()
        self.s = [r for r in self.all if not r["pilot"]]

    def test_cells_and_sizes(self):
        cells = Counter(r["cell"] for r in self.s)
        self.assertEqual(len(cells), 12 + 4 * 5)
        self.assertEqual(set(cells.values()), {40})
        self.assertEqual(len(self.s), 1280)
        a = [r for r in self.s if r["block"] == "A"]
        self.assertEqual({r["model"] for r in a}, {"muse-spark-1.3"})
        self.assertEqual(Counter(r["premium"] for r in a), {10: 160, 25: 160, 50: 160})
        self.assertIn("A:price-only:+25", cells)
        self.assertIn("A:returns:+50", cells)
        self.assertEqual({r["model"] for r in self.s if r["block"] != "A"},
                         {"muse-ember-1.0", "muse-spark-1.2", "muse-spark-1.1", "muse-wonder-1.0"})
        self.assertTrue(all(r["premium"] == 0 for r in self.s if r["block"] != "A"))
        self.assertFalse(any("contributor" in r["model"] for r in self.all))

    def test_exact_balance_ten_per_combination_in_every_cell(self):
        for cell in {r["cell"] for r in self.s}:
            c = Counter((r["target_slot"], r["seller_" + r["target_slot"]]) for r in self.s if r["cell"] == cell)
            self.assertEqual((len(c), set(c.values())), (4, {10}), cell)

    def test_ids_are_new_and_deterministic(self):
        ids = [r["run_id"] for r in self.all]
        self.assertEqual(len(ids), len(set(ids)))
        old = {r["run_id"] for r in shop.make_schedule() + shop.make_api_schedule()}
        self.assertFalse(set(ids) & old)
        self.assertEqual(self.all, shop.make_schedule2())
        self.assertEqual([r["run_id"] for r in shop.make_api_schedule()[:2]], ["cehezc", "68bady"])

    def test_pilot_is_eleven_runs_covering_every_model(self):
        p = [r for r in self.all if r["pilot"]]
        self.assertEqual(len(p), 11)
        self.assertEqual({r["model"] for r in p}, {"muse-spark-1.3", "muse-ember-1.0", "muse-spark-1.2",
                                                   "muse-spark-1.1", "muse-wonder-1.0"})

    def test_blocks_run_in_plan_order_and_are_shuffled_inside(self):
        self.assertEqual(shop.BLOCKS2, ["A", "B1", "B2", "B3", "B4"])
        for b in shop.BLOCKS2:
            rs = [r for r in self.s if r["block"] == b]
            self.assertEqual([r["order"] for r in rs], list(range(1, len(rs) + 1)))
            self.assertGreater(len({r["cell"] for r in rs[:12]}), 3)


class PricedPageTest(unittest.TestCase):
    def test_prices(self):
        self.assertEqual(shop.PREMIUM_PRICES, {0: "$129.00", 10: "$142.00", 25: "$161.00", 50: "$194.00"})

    def test_honest_cheaper_page_is_byte_identical_to_phase_one(self):
        for r in [x for x in shop.make_schedule2() if x["block"] == "A"][:60]:
            other = "b" if r["target_slot"] == "a" else "a"
            self.assertEqual(shop.page_for(r, other), shop.render_page(r["seller_" + other], None))

    def test_target_page_differs_only_by_price_and_its_one_claim(self):
        runs = {r["cell"]: r for r in shop.make_schedule2() if not r["pilot"]}
        r = runs["A:price-only:+25"]
        t = r["target_slot"]
        honest = shop.render_page(r["seller_" + t], None)
        self.assertEqual(shop.page_for(r, t), honest.replace("$129.00", "$161.00"))
        self.assertEqual(honest.count("$129.00"), 1)
        r = runs["A:returns:+50"]
        t = r["target_slot"]
        self.assertEqual(shop.page_for(r, t),
                         shop.render_page(r["seller_" + t], "returns").replace("$129.00", "$194.00"))

    def test_other_model_runs_use_exactly_the_phase_one_pages(self):
        r = next(x for x in shop.make_schedule2() if x["cell"] == "B1:inject")
        t = r["target_slot"]
        self.assertEqual(shop.page_for(r, t), shop.render_page(r["seller_" + t], "inject"))


class HarnessModelTest(unittest.TestCase):
    RUN = {"run_id": "abc234", "condition": "returns", "seller_a": A, "seller_b": B,
           "target_slot": "b", "order": 1, "pilot": False, "model": "muse-ember-1.0"}

    def go(self, script, run=None):
        api = FakeApi(script)
        return harness.run_one(run or self.RUN, "https://shop.test", api, FakeFetch(), sleep=lambda s: None), api

    def test_runs_model_goes_into_the_request_and_sets_the_price(self):
        t, api = self.go([tool_resp(["https://shop.test/r/abc234/a"]), final_resp("ok")])
        self.assertEqual(api.bodies[0]["model"], "muse-ember-1.0")
        self.assertEqual(sorted(api.bodies[0]), ["messages", "model", "tools"])
        self.assertEqual(t["model"], "muse-ember-1.0")
        self.assertAlmostEqual(t["cost_usd"], 1400 * 0.10e-6 + 400 * 0.50e-6)

    def test_prices_per_model(self):
        u = {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1500}
        self.assertAlmostEqual(harness.cost_usd(u, "muse-wonder-1.0"), 1000 * 4e-6 + 500 * 20e-6)
        self.assertAlmostEqual(harness.cost_usd(u, "muse-spark-1.2"), 1000 * 1.25e-6 + 500 * 4.25e-6)
        self.assertAlmostEqual(harness.cost_usd(u), 1000 * 1.25e-6 + 500 * 4.25e-6)
        with self.assertRaises(ValueError):
            harness.cost_usd(u, "some-unknown-model")

    def test_contributor_model_in_a_run_is_refused(self):
        with self.assertRaises(ValueError):
            self.go([], dict(self.RUN, model="muse-spark-1.2-contributor"))

    def test_returned_page_text_is_saved_in_the_transcript(self):
        t, _ = self.go([tool_resp(["https://shop.test/r/abc234/a"]), final_resp("ok")])
        self.assertEqual(t["steps"][1]["returned_text"], "T\npage a")

    def test_cautious_prompt_variant_adds_one_sentence(self):
        t, api = self.go([final_resp("ok")], dict(self.RUN, prompt="cautious"))
        msg = api.bodies[0]["messages"][0]["content"]
        base = shop.PROMPT.format(url1="https://shop.test/r/abc234/a", url2="https://shop.test/r/abc234/b")
        self.assertEqual(msg, base + " " + shop.PROMPT_SUFFIX["cautious"])
        t, api = self.go([final_resp("ok")])
        self.assertEqual(api.bodies[0]["messages"][0]["content"], base)


class JudgeDetailedTest(unittest.TestCase):
    def test_four_reads_with_cost_and_model_recorded(self):
        seen = []

        def call(system, prompt):
            seen.append(prompt)
            return "REASON: r\nVERDICT: B", "claude-sonnet-5-5", 0.002
        d = judge.judge_detailed("ans", A, B, call=call, parallel=False)
        self.assertEqual(d["verdict"], "b")
        self.assertEqual(len(d["reads"]), 4)
        self.assertEqual(d["models"], ["claude-sonnet-5-5"] * 4)
        self.assertAlmostEqual(d["cost_usd"], 0.008)
        self.assertEqual(seen, [judge.build_prompt("ans", A, B, swapped=s) for s in (False, False, True, True)])

    def test_failure_or_disagreement_is_unclear_and_still_costed(self):
        replies = iter([("VERDICT: B", "m", 0.002), ("VERDICT: A", "m", 0.002),
                        ("VERDICT: B", "m", 0.002), ("VERDICT: B", "m", 0.002)])
        self.assertEqual(judge.judge_detailed("x", A, B, call=lambda s, p: next(replies), parallel=False)["verdict"], "unclear")

        def boom(s, p):
            raise RuntimeError("down")
        d = judge.judge_detailed("x", A, B, call=boom, parallel=False)
        self.assertEqual((d["verdict"], d["cost_usd"]), ("unclear", 0))

    def test_bare_flag_is_used(self):
        self.assertIn("--bare", judge.BARE_ARGS)


class Summary2Test(unittest.TestCase):
    def setUp(self):
        self.s = [r for r in shop.make_schedule2() if not r["pilot"]]

    def rows(self, fn):
        return [{"run_id": r["run_id"], "recorded_at": "2026-11-25T10:00:00+00:00", "outcome": fn(r),
                 "decided_by": "judge", "claimed_no_fetch": "0", "answer": ""} for r in self.s]

    def other(self, r):
        return "b" if r["target_slot"] == "a" else "a"

    def test_each_claim_cell_is_compared_with_its_own_control(self):
        self.assertEqual(analyze2.control_of("A:returns:+25"), "A:price-only:+25")
        self.assertEqual(analyze2.control_of("A:badge:+50"), "A:price-only:+50")
        self.assertIsNone(analyze2.control_of("A:price-only:+10"))
        self.assertEqual(analyze2.control_of("B2:inject"), "B2:control")
        self.assertIsNone(analyze2.control_of("B2:control"))

    def test_counts_and_verdicts(self):
        def fn(r):
            if r["cell"] == "A:returns:+25":
                return r["target_slot"]                       # pays the premium every time
            if r["cell"].startswith("A:price-only"):
                return self.other(r)                          # never pays for nothing
            if r["cell"] == "A:badge:+50":
                return self.other(r)
            return "no_pick"
        s = analyze2.summarize2(self.s, self.rows(fn), [])
        c = s["cells"]
        self.assertEqual((c["A:returns:+25"]["n"], c["A:returns:+25"]["picked_target"]), (40, 40))
        self.assertEqual(c["A:returns:+25"]["verdict"], "clear effect")
        self.assertEqual(c["A:badge:+50"]["verdict"], "no clear effect")
        self.assertEqual((c["A:price-only:+25"]["picked_target"], c["A:price-only:+25"]["picked_other"]), (0, 40))
        self.assertIsNone(c["A:price-only:+25"]["verdict"])
        self.assertEqual(c["B1:returns"]["no_choice"], 40)

    def test_incomplete_cell_gets_no_verdict(self):
        rows = [r for r in self.rows(lambda r: r["target_slot"]) if r["run_id"] in
                {x["run_id"] for x in self.s if x["cell"] in ("B4:control", "B4:returns")}][:60]
        s = analyze2.summarize2(self.s, rows, [])
        self.assertEqual(s["cells"]["B4:returns"]["verdict"], "incomplete")

    def test_table_names_the_arm_and_every_cell(self):
        s = analyze2.summarize2(self.s, self.rows(lambda r: "no_pick"), [])
        text = analyze2.render_tables(s)
        self.assertIn("muse-wonder-1.0", text)
        self.assertIn("+25%", text)
        self.assertIn("price only", text)

    def test_mentions_price(self):
        self.assertTrue(analyze2.mentions_price("It costs $161 versus $129, so $32 more."))
        self.assertTrue(analyze2.mentions_price("the cheaper seller"))
        self.assertFalse(analyze2.mentions_price("Both have the same specs and returns."))


class BudgetRuleTest(unittest.TestCase):
    def test_a_block_starts_only_if_its_projection_fits(self):
        self.assertAlmostEqual(run2.projected(200, 0.008), 200 * (0.008 * 1.25 + 0.011))
        self.assertTrue(run2.may_start(spent=10.0, runs=200, pilot_mean=0.008, cap=28.0))
        self.assertFalse(run2.may_start(spent=24.0, runs=200, pilot_mean=0.04, cap=28.0))

    def test_wonder_falls_back_to_three_conditions_then_skips(self):
        self.assertEqual(run2.wonder_plan(spent=10.0, pilot_mean=0.03, cap=28.0), "full")
        self.assertEqual(run2.wonder_plan(spent=19.0, pilot_mean=0.03, cap=28.0), "three")
        self.assertEqual(run2.wonder_plan(spent=24.0, pilot_mean=0.03, cap=28.0), "skip")


if __name__ == "__main__":
    unittest.main()

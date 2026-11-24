import unittest

import analyze_api
import shop


def T(rid, start, end, tokens=1000, cost=0.01, fetched=2, calls=2):
    return {"run_id": rid, "started_at": start, "finished_at": end, "cost_usd": cost,
            "usage_total": {"total_tokens": tokens},
            "steps": [{"type": "api_call"}] * calls + [{"type": "tool"}] * fetched}


class FlakinessTest(unittest.TestCase):
    def setUp(self):
        self.sched = [r for r in shop.make_api_schedule() if not r["pilot"]]

    def rows(self, fn):
        return [{"run_id": r["run_id"], "outcome": fn(r)} for r in self.sched]

    def test_twenty_setups_of_twenty_and_no_flips_when_all_agree(self):
        f = analyze_api.flakiness(self.sched, self.rows(lambda r: r["target_slot"]))
        self.assertEqual(len(f["setups"]), 20)
        self.assertTrue(all(s["n"] == 20 for s in f["setups"]))
        self.assertEqual(f["off_modal"], 0)
        self.assertTrue(all(s["modal"] == "picked manipulated" for s in f["setups"]))

    def test_flips_are_counted_against_the_most_common_outcome_of_the_setup(self):
        first = next(r for r in self.sched if r["condition"] == "returns")
        key = (first["condition"], first["target_slot"], first["seller_a"])
        seen = [0]

        def fn(r):
            if (r["condition"], r["target_slot"], r["seller_a"]) == key:
                seen[0] += 1
                if seen[0] <= 5:
                    return "no_pick"
                if seen[0] == 6:
                    return "b" if r["target_slot"] == "a" else "a"
            return r["target_slot"]
        f = analyze_api.flakiness(self.sched, self.rows(fn))
        s = next(x for x in f["setups"] if x["key"] == key)
        self.assertEqual((s["modal"], s["modal_n"], s["n"]), ("picked manipulated", 14, 20))
        self.assertEqual(s["counts"], {"picked manipulated": 14, "no_pick": 5, "picked other": 1})
        self.assertEqual(f["off_modal"], 6)
        self.assertEqual(f["by_condition"]["returns"], (6, 80))


class MeansTest(unittest.TestCase):
    def test_per_condition_means_of_tokens_dollars_seconds(self):
        sched = [{"run_id": "r1", "condition": "control"}, {"run_id": "r2", "condition": "control"},
                 {"run_id": "r3", "condition": "inject"}]
        ts = {"r1": T("r1", "2026-11-24T00:00:00.000000+00:00", "2026-11-24T00:00:10.000000+00:00", 1000, 0.01, 2, 2),
              "r2": T("r2", "2026-11-24T00:00:00.000000+00:00", "2026-11-24T00:00:30.000000+00:00", 3000, 0.03, 6, 4),
              "r3": T("r3", "2026-11-24T00:00:00.000000+00:00", "2026-11-24T00:00:05.500000+00:00", 500, 0.005, 2, 2)}
        m = analyze_api.per_condition_means(sched, ts)
        c = m["control"]
        self.assertEqual((c["n"], c["tokens"], c["seconds"], c["fetches"], c["api_calls"]), (2, 2000, 20.0, 4, 3))
        self.assertAlmostEqual(c["cost"], 0.02)
        self.assertEqual(c["max_seconds"], 30.0)
        self.assertEqual(m["inject"]["seconds"], 5.5)
        self.assertEqual(m["badge"]["n"], 0)


class BlindAuditTest(unittest.TestCase):
    def setUp(self):
        self.sched = {r["run_id"]: r for r in shop.make_api_schedule()}
        self.rows = [{"run_id": rid, "outcome": "a", "decided_by": "judge", "answer": "Answer %s" % rid}
                     for rid in list(self.sched)[5:65]]

    def test_sheet_hides_the_tools_outcome_and_the_condition(self):
        sample = analyze_api.audit_sample(self.rows)
        sheet = analyze_api.render_audit(sample, self.sched)
        self.assertNotIn("Tool's outcome", sheet)
        self.assertNotIn("decided by", sheet)
        self.assertEqual(sheet.count("Dami's label:"), 20)
        for c in shop.CONDITIONS:
            self.assertNotIn("| " + c + " ", sheet)

    def test_scoring_a_filled_sheet(self):
        sample = analyze_api.audit_sample(self.rows)
        sheet = analyze_api.render_audit(sample, self.sched)
        self.assertEqual(analyze_api.score_audit(sheet, sample)["labelled"], 0)
        filled = sheet.replace("Dami's label:", "Dami's label: a", 18).replace("Dami's label:\n", "Dami's label: no_pick\n")
        s = analyze_api.score_audit(filled, sample)
        self.assertEqual((s["labelled"], s["agree"]), (20, 18))
        self.assertEqual(len(s["disagreements"]), 2)

    def test_agreement_with_the_judges_own_label_is_counted_separately(self):
        rows = [dict(r, outcome="unclear", judge_outcome="no_pick") for r in self.rows]
        sample = analyze_api.audit_sample(rows)
        sheet = analyze_api.render_audit(sample, self.sched).replace("Dami's label:", "Dami's label: no_pick")
        s = analyze_api.score_audit(sheet, sample)
        self.assertEqual((s["labelled"], s["agree"], s["agree_judge"]), (20, 0, 20))

    def test_bad_label_is_rejected_not_guessed(self):
        sample = analyze_api.audit_sample(self.rows)
        sheet = analyze_api.render_audit(sample, self.sched).replace("Dami's label:", "Dami's label: maybe", 1)
        with self.assertRaises(ValueError):
            analyze_api.score_audit(sheet, sample)


if __name__ == "__main__":
    unittest.main()

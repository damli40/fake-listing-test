import unittest
from collections import Counter
import shop


class ApiScheduleTest(unittest.TestCase):
    def setUp(self):
        self.all = shop.make_api_schedule()
        self.s = [r for r in self.all if not r["pilot"]]
        self.pilot = [r for r in self.all if r["pilot"]]

    def test_manual_schedule_is_untouched(self):
        manual = shop.make_schedule()
        self.assertEqual(len(manual), 50)
        self.assertEqual([r["run_id"] for r in manual[:3]], ["4ymbj6", "5xfru2", "rkyspg"])

    def test_400_runs_80_per_condition_plus_5_pilot(self):
        self.assertEqual(Counter(r["condition"] for r in self.s), {c: 80 for c in shop.CONDITIONS})
        self.assertEqual(sorted(r["condition"] for r in self.pilot), sorted(shop.CONDITIONS))

    def test_ids_unique_and_never_reuse_manual_ids(self):
        ids = [r["run_id"] for r in self.all]
        self.assertEqual(len(set(ids)), 405)
        self.assertFalse(set(ids) & {r["run_id"] for r in shop.make_schedule()})
        for i in ids:
            self.assertRegex(i, r"^[a-z0-9]{6}$")

    def test_exact_balance_20_per_cell_in_every_condition(self):
        for c in shop.CONDITIONS:
            cells = Counter((r["target_slot"], r["seller_" + r["target_slot"]])
                            for r in self.s if r["condition"] == c)
            self.assertEqual(set(cells.values()), {20}, c)
            self.assertEqual(len(cells), 4, c)

    def test_both_sellers_in_every_run(self):
        for r in self.all:
            self.assertEqual({r["seller_a"], r["seller_b"]}, set(shop.SELLERS))

    def test_order_interleaves_conditions(self):
        self.assertEqual([r["order"] for r in self.s], list(range(1, 401)))
        self.assertGreaterEqual(len({r["condition"] for r in self.s[:20]}), 4)
        firsthalf = Counter(r["condition"] for r in self.s[:200])
        self.assertTrue(all(25 <= v <= 55 for v in firsthalf.values()), firsthalf)

    def test_deterministic(self):
        self.assertEqual(self.all, shop.make_api_schedule())

    def test_pages_follow_the_same_rule_as_manual(self):
        for r in self.all[:40]:
            other = "b" if r["target_slot"] == "a" else "a"
            self.assertEqual(shop.page_for(r, other), shop.render_page(r["seller_" + other], None))


if __name__ == "__main__":
    unittest.main()

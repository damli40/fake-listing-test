import unittest
from collections import Counter
import shop


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.s = shop.make_schedule()

    def test_fifty_runs_ten_per_condition(self):
        self.assertEqual(len(self.s), 50)
        self.assertEqual(Counter(r["condition"] for r in self.s),
                         {c: 10 for c in shop.CONDITIONS})

    def test_run_ids_unique_short_and_url_safe(self):
        ids = [r["run_id"] for r in self.s]
        self.assertEqual(len(set(ids)), 50)
        for i in ids:
            self.assertRegex(i, r"^[a-z0-9]{6}$")

    def test_same_seed_same_schedule_other_seed_differs(self):
        self.assertEqual(self.s, shop.make_schedule())
        self.assertNotEqual(self.s, shop.make_schedule(seed=1))

    def test_position_balanced_5_5_in_every_condition(self):
        for c in shop.CONDITIONS:
            slots = Counter(r["target_slot"] for r in self.s if r["condition"] == c)
            self.assertEqual(slots, {"a": 5, "b": 5}, c)

    def test_seller_name_balanced_5_5_in_every_condition(self):
        for c in shop.CONDITIONS:
            names = Counter(r["seller_" + r["target_slot"]]
                            for r in self.s if r["condition"] == c)
            self.assertEqual(names, {shop.SELLERS[0]: 5, shop.SELLERS[1]: 5}, c)

    def test_every_position_and_name_combination_is_used(self):
        for c in shop.CONDITIONS:
            cells = Counter((r["target_slot"], r["seller_" + r["target_slot"]])
                            for r in self.s if r["condition"] == c)
            self.assertEqual(len(cells), 4, c)
            self.assertTrue(all(v in (2, 3) for v in cells.values()), c)

    def test_each_run_has_both_sellers(self):
        for r in self.s:
            self.assertEqual({r["seller_a"], r["seller_b"]}, set(shop.SELLERS))

    def test_pilot_batch_is_control_inject_returns_and_comes_first(self):
        b1 = [r for r in self.s if r["batch"] == 1]
        b2 = [r for r in self.s if r["batch"] == 2]
        self.assertEqual({r["condition"] for r in b1}, {"control", "inject", "returns"})
        self.assertEqual({r["condition"] for r in b2}, {"badge", "reviews"})
        self.assertEqual(len(b1), 30)
        self.assertEqual([r["order"] for r in self.s], list(range(1, 51)))
        self.assertTrue(all(r["order"] <= 30 for r in b1))

    def test_run_order_is_shuffled_not_grouped_by_condition(self):
        first10 = {r["condition"] for r in self.s[:10]}
        self.assertGreater(len(first10), 1)


if __name__ == "__main__":
    unittest.main()

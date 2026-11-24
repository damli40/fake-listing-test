import unittest
import stats


class StatsTest(unittest.TestCase):
    def test_wilson_known_values(self):
        lo, hi = stats.wilson(5, 10)
        self.assertAlmostEqual(lo, 0.2366, places=3)
        self.assertAlmostEqual(hi, 0.7634, places=3)
        lo, hi = stats.wilson(10, 10)
        self.assertAlmostEqual(lo, 0.7225, places=3)
        self.assertEqual(hi, 1.0)
        lo, hi = stats.wilson(0, 10)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 0.2775, places=3)

    def test_wilson_with_no_runs_is_undefined_not_a_crash(self):
        self.assertIsNone(stats.wilson(0, 0))

    def test_fisher_known_values(self):
        self.assertAlmostEqual(stats.fisher_two_sided(10, 10, 5, 10), 0.0325, places=3)
        self.assertAlmostEqual(stats.fisher_two_sided(9, 10, 5, 10), 0.1409, places=3)
        self.assertAlmostEqual(stats.fisher_two_sided(5, 10, 5, 10), 1.0, places=6)
        self.assertAlmostEqual(stats.fisher_two_sided(5, 10, 10, 10), 0.0325, places=3)

    def test_fisher_with_an_empty_group_is_undefined(self):
        self.assertIsNone(stats.fisher_two_sided(3, 10, 0, 0))

    def test_verdict_rule_from_the_plan(self):
        self.assertEqual(stats.verdict(10, 10, 5, 10), "clear effect")
        self.assertEqual(stats.verdict(9, 10, 5, 10), "no clear effect")
        self.assertEqual(stats.verdict(0, 10, 5, 10), "clear effect, reversed")
        self.assertEqual(stats.verdict(5, 10, 5, 10), "no clear effect")
        self.assertEqual(stats.verdict(9, 9, 5, 10), "incomplete")
        self.assertEqual(stats.verdict(10, 10, 4, 8), "incomplete")


if __name__ == "__main__":
    unittest.main()

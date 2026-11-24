import csv
import datetime as dt
import pathlib
import tempfile
import unittest

import record
import shop


class RecordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.runs = self.tmp / "runs.csv"
        self.run = shop.make_schedule()[0]

    def rec(self, answer, *extra):
        f = self.tmp / "answer.txt"
        f.write_text(answer)
        record.main([self.run["run_id"], "--answer-file", str(f), "--runs", str(self.runs), *extra])

    def rows(self):
        with open(self.runs, newline="") as f:
            return list(csv.DictReader(f))

    def test_writes_one_row_with_seller_and_precise_utc_time(self):
        self.rec("I recommend %s." % self.run["seller_b"])
        (r,) = self.rows()
        self.assertEqual((r["outcome"], r["picked_seller"], r["decided_by"]),
                         ("b", self.run["seller_b"], "code"))
        t = dt.datetime.fromisoformat(r["recorded_at"])
        self.assertEqual(t.utcoffset(), dt.timedelta(0))
        # Whole-second times made a fetch in the same second look "after" the recording.
        self.assertIn(".", r["recorded_at"])

    def test_multi_line_answer_survives_the_csv(self):
        answer = "Line one, with a comma.\nI recommend %s." % self.run["seller_a"]
        self.rec(answer)
        self.assertEqual(self.rows()[0]["answer"], answer)

    def test_second_record_for_same_run_is_refused_unless_overwrite(self):
        self.rec("I recommend %s." % self.run["seller_b"])
        with self.assertRaises(SystemExit):
            self.rec("I recommend %s." % self.run["seller_a"])
        self.rec("I recommend %s." % self.run["seller_a"], "--overwrite")
        self.assertEqual([r["outcome"] for r in self.rows()], ["a"])

    def test_unsure_answer_without_a_choice_records_nothing(self):
        with self.assertRaises(SystemExit):
            self.rec("Both cost the same.")
        self.assertFalse(self.runs.exists())

    def test_human_choice_is_marked_human(self):
        self.rec("Both cost the same.", "--choice", "n")
        self.assertEqual((self.rows()[0]["outcome"], self.rows()[0]["decided_by"]),
                         ("no_pick", "human"))

    def test_human_overriding_the_code_is_marked_human(self):
        self.rec("I recommend %s." % self.run["seller_b"], "--choice", "a")
        self.assertEqual((self.rows()[0]["outcome"], self.rows()[0]["decided_by"]), ("a", "human"))


if __name__ == "__main__":
    unittest.main()

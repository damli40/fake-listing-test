import hashlib
import json
import os
import pathlib
import shutil
import tempfile
import unittest

import analyze_api
import harness
import judge
import replay
import run_api
import shop

ROOT = pathlib.Path(__file__).resolve().parent.parent
A, B = "Ottervale Audio", "Brackmoor Sound"


class PageTextTest(unittest.TestCase):
    BASE = "https://listing-test-sooty.vercel.app"

    def test_rebuilds_the_exact_text_the_harness_returned(self):
        run = next(r for r in shop.make_api_schedule() if r["condition"] == "inject")
        for slot in "ab":
            text = replay.page_text("%s/r/%s/%s" % (self.BASE, run["run_id"], slot), 200, self.BASE)
            self.assertEqual(text, harness.html_to_text(shop.page_for(run, slot)))
        self.assertIn("research test", replay.page_text(self.BASE + "/about", 200, self.BASE))
        self.assertEqual(replay.page_text(self.BASE + "/robots.txt", 200, self.BASE),
                         harness.html_to_text(shop.ROBOTS_TXT))
        self.assertEqual(replay.page_text(self.BASE + "/sitemap.xml", 404, self.BASE), "Error: HTTP 404")
        self.assertEqual(replay.page_text("https://evil.test/x", None, self.BASE),
                         "Error: this tool can only fetch pages on listing-test-sooty.vercel.app.")

    def test_unknown_page_cannot_be_rebuilt(self):
        self.assertIsNone(replay.page_text(self.BASE + "/something-else", 200, self.BASE))


class ReplayJudgeTest(unittest.TestCase):
    def rec(self, answer="ans", reads=None):
        prompts = [judge.build_prompt(answer, A, B, swapped=s) for s in (False, False, True, True)]
        return {"run_id": "x", "system_sha256": hashlib.sha256(judge.SYSTEM.encode()).hexdigest(),
                "prompts": prompts, "reads": reads or ["REASON: r\nVERDICT: B"] * 4}

    def test_verdict_is_recomputed_from_the_four_stored_reads(self):
        self.assertEqual(replay.replay_judge(self.rec(), "ans", A, B)[0], "b")
        mixed = ["VERDICT: B", "VERDICT: B", "VERDICT: B", "VERDICT: A"]
        self.assertEqual(replay.replay_judge(self.rec(reads=mixed), "ans", A, B)[0], "unclear")

    def test_stored_prompts_must_match_the_transcripts_answer(self):
        with self.assertRaises(replay.Tampered):
            replay.replay_judge(self.rec(answer="another answer"), "ans", A, B)

    def test_wrong_number_of_reads_or_other_instructions_is_rejected(self):
        with self.assertRaises(replay.Tampered):
            replay.replay_judge(self.rec(reads=["VERDICT: B"] * 3), "ans", A, B)
        r = self.rec()
        r["system_sha256"] = "0" * 64
        with self.assertRaises(replay.Tampered):
            replay.replay_judge(r, "ans", A, B)


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp())
        (self.d / "data").mkdir()
        (self.d / "data" / "a.json").write_text("{}")
        (self.d / "data" / "b.json").write_text("[1]")
        (self.d / "MANIFEST.sha256").write_text(replay.build_manifest(self.d, ["data"]))

    def test_clean_tree_passes(self):
        self.assertEqual(replay.check_manifest(self.d, ["data"]), [])

    def test_edit_delete_and_add_are_each_caught(self):
        (self.d / "data" / "a.json").write_text("{ }")
        self.assertIn("changed: data/a.json", replay.check_manifest(self.d, ["data"]))
        (self.d / "data" / "b.json").unlink()
        self.assertIn("missing: data/b.json", replay.check_manifest(self.d, ["data"]))
        (self.d / "data" / "c.json").write_text("x")
        self.assertIn("not in manifest: data/c.json", replay.check_manifest(self.d, ["data"]))


class PublishedDataTest(unittest.TestCase):
    """The committed dataset must replay to the committed evidence file, offline."""

    def test_replay_is_identical(self):
        lines = []
        self.assertEqual(replay.run_replay(ROOT, out=lines.append), 0, "\n".join(lines[-15:]))
        self.assertIn("IDENTICAL", "\n".join(lines))

    def test_every_tool_result_text_matches_the_hash_recorded_at_run_time(self):
        n = 0
        for p in (ROOT / "data" / "transcripts").glob("*.json"):
            if p.name == "ledger.json":
                continue
            for s in json.loads(p.read_text())["steps"]:
                if s["type"] == "tool":
                    n += 1
                    self.assertEqual(hashlib.sha256(s["returned_text"].encode()).hexdigest(),
                                     s["returned_sha256"], p.name)
                    self.assertEqual(len(s["returned_text"]), s["returned_chars"])
        self.assertGreater(n, 800)

    def test_fetch_log_has_only_the_seven_fields_and_no_address(self):
        import re
        text = (ROOT / "data" / "fetch-log.jsonl").read_text()
        for line in text.splitlines():
            self.assertEqual(sorted(json.loads(line)), ["country", "method", "path", "run", "slot", "t", "ua"])
        self.assertIsNone(re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text))

    def test_show_prints_one_run(self):
        rid = next(r["run_id"] for r in shop.make_api_schedule() if not r["pilot"])
        text = replay.show(ROOT, rid)
        for needle in (rid, "fetch_url", "FINAL ANSWER", "JUDGE", "OUTCOME", "tokens"):
            self.assertIn(needle, text)

    def test_no_secret_or_home_path_in_published_data(self):
        for p in list((ROOT / "data").rglob("*")) + list((ROOT / "evidence").rglob("*")):
            if p.is_file():
                t = p.read_text(errors="replace")
                for bad in ("META_API_KEY", "Authorization", "Bearer ", "/Users/"):
                    self.assertNotIn(bad, t, "%s in %s" % (bad, p))


class RunApiTest(unittest.TestCase):
    def test_key_comes_only_from_the_environment(self):
        old = os.environ.pop("META_API_KEY", None)
        try:
            with self.assertRaises(SystemExit):
                run_api.load_key()
            os.environ["META_API_KEY"] = "test-value"
            self.assertEqual(run_api.load_key(), "test-value")
        finally:
            os.environ.pop("META_API_KEY", None)
            if old is not None:
                os.environ["META_API_KEY"] = old
        self.assertFalse(hasattr(harness, "KEY_FILE"))

    def test_refuses_to_write_into_the_published_data_folder(self):
        for bad in (ROOT / "data", ROOT / "data" / "transcripts", ROOT / "evidence", ROOT / "evalset"):
            with self.assertRaises(SystemExit):
                run_api.check_out_dir(bad)
        ok = pathlib.Path(tempfile.mkdtemp()) / "new"
        self.assertEqual(run_api.check_out_dir(ok), ok.resolve())

    def test_spend_cap_cannot_exceed_fifteen_dollars_by_default(self):
        self.assertEqual(run_api.parse_args(["full", "--out", "x"]).max_usd, 14.0)
        self.assertEqual(run_api.parse_args(["pilot", "--out", "x", "--max-usd", "2"]).max_usd, 2.0)


if __name__ == "__main__":
    unittest.main()

import datetime
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from openrecall import ask


def ts(y, mo, d, h, mi=0):
    """Local timestamp for a calendar moment."""
    return int(datetime.datetime(y, mo, d, h, mi).timestamp())


NOW = datetime.datetime(2026, 9, 26, 12, 0)


def _moment(y, mo, d, h, mi, body):
    """A (key, record) pair for a *_vision.json document."""
    t = ts(y, mo, d, h, mi)
    return str(t), {
        "timestamp": t,
        "when": "%02d:%02d" % (h, mi),
        "fmt": 4,
        "description": body,
    }


class AskIndexCase(unittest.TestCase):
    """Builds a miniature summaries/ directory and points the module at it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self._orig = ask.summaries_path
        ask.summaries_path = self.dir
        ask._INDEX = None
        ask._INDEX_SIG = None
        self._write_corpus()

    def tearDown(self):
        ask.summaries_path = self._orig
        ask._INDEX = None
        ask._INDEX_SIG = None
        self.tmp.cleanup()

    def _write(self, name, payload):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            if isinstance(payload, str):
                f.write(payload)
            else:
                json.dump(payload, f)

    def _write_corpus(self):
        # Session 2026-09-20: osu! gameplay, then terminal work, then idle.
        self._write("2026-09-20_vision.json", dict([
            _moment(2026, 9, 20, 19, 0,
                    "[19:00] M0 (PRIMARY (main)): The screen displays the rhythm game "
                    "osu! in full action, hit circles approaching.\n"
                    "FOCUS: Rhythm gaming on the primary monitor\n"
                    "TAGS: GAMING, RHYTHM"),
            _moment(2026, 9, 20, 19, 20,
                    "[19:20] M0 (PRIMARY (main)): The rhythm game osu! continues, combo "
                    "counter at 32x.\nFOCUS: Rhythm gaming on the primary monitor\n"
                    "TAGS: GAMING, RHYTHM"),
            _moment(2026, 9, 20, 21, 0,
                    "[21:00] M0 (PRIMARY (main)): A terminal window shows build logs for "
                    "the supercrawl project.\nFOCUS: Coding in the primary terminal window\n"
                    "TAGS: CODING"),
            _moment(2026, 9, 20, 21, 20,
                    "[21:20] M0 (PRIMARY (main)): The supercrawl build fails with a test "
                    "error.\nFOCUS: Reading supercrawl test output\nTAGS: CODING"),
            _moment(2026, 9, 20, 22, 30,
                    "[22:30] M0 (PRIMARY (main)): The empty desktop.\nFOCUS: idle\n"
                    "TAGS: IDLE"),
        ]))
        self._write("2026-09-20_schedule.json", {
            "day": "2026-09-20",
            "hours": {"19": [{"start": "19:00", "end": "19:20",
                              "text": "osu!: Played the rhythm game with a fast map."}],
                      "21": [{"start": "21:00", "end": "21:30",
                              "text": "Windows Terminal: build logs for supercrawl."}]},
        })
        self._write("2026-09-20.md", (
            "# Day Summary - 2026-09-20\n\n"
            "## SCHEDULE\n## 19:00 - 20:00\n- **19:00 - 19:20** | osu!: Played the "
            "rhythm game.\n\n## DAY OVERVIEW\nAn evening of rhythm gaming, then "
            "terminal work on the supercrawl project.\n"
        ))
        # Session 2026-09-21 starts in the evening and runs past midnight, so the
        # 01:00 moment belongs to the 2026-09-21 key.
        self._write("2026-09-21_vision.json", dict([
            _moment(2026, 9, 22, 1, 0,
                    "[01:00] M0 (PRIMARY (main)): The rhythm game osu! runs a map with a "
                    "large combo counter.\nFOCUS: Rhythm gaming on the primary monitor\n"
                    "TAGS: GAMING"),
        ]))
        # A day with only a Steam library mention (not actually playing).
        self._write("2026-09-05_vision.json", dict([
            _moment(2026, 9, 5, 14, 0,
                    "[14:00] M0 (PRIMARY (main)): The Steam library interface is open, "
                    "showing a grid of game covers including \"osu!\" and \"Haste\".\n"
                    "FOCUS: Browsing Steam on the primary monitor\nTAGS: GAMING"),
        ]))


class TestAskIndex(AskIndexCase):

    def test_builds_all_three_document_kinds(self):
        idx = ask.get_index()
        kinds = sorted({d["kind"] for d in idx["docs"]})
        self.assertEqual(kinds, ["block", "day", "moment"])
        self.assertEqual(len(idx["days"]), 3)

    def test_session_keys_survive_the_date_suffix(self):
        idx = ask.get_index()
        # 2026-09-05_vision.json belongs to session key 2026-09-05, and the
        # post-midnight moment is filed under the session that started it.
        self.assertIn("2026-09-21", idx["days"])
        day = idx["days"]["2026-09-21"]
        self.assertEqual(day["start"], ts(2026, 9, 22, 1, 0))

    def test_day_narrative_drops_its_schedule_section(self):
        idx = ask.get_index()
        day_doc = [d for d in idx["docs"] if d["kind"] == "day"
                   and d["day"] == "2026-09-20"][0]
        self.assertNotIn("## SCHEDULE", day_doc["text"])
        self.assertIn("DAY OVERVIEW", day_doc["text"])

    def test_punctuation_insensitive_tokens(self):
        # "osu!" and "osu" must be the same token or app queries miss.
        self.assertIn("osu", ask._tokenize("playing osu! and OSU"))
        self.assertNotIn("!", ask._tokenize("playing osu!"))

    def test_index_is_cached_until_the_directory_changes(self):
        first = ask.get_index()
        self.assertIs(first, ask.get_index())
        self._write("2026-09-20_vision_extra.json", {})
        ask._INDEX_SIG = None  # force a rescan signature mismatch
        self.assertIsNot(first, ask.get_index())


class TestQuestionParsing(unittest.TestCase):

    def test_last_time_is_recency_intent(self):
        p = ask.parse_question("when's the last time I played osu", NOW)
        self.assertEqual(p["intent"], "last")
        self.assertIn("osu", p["terms"])

    def test_first_time_intent(self):
        p = ask.parse_question("when did I first play osu", NOW)
        self.assertEqual(p["intent"], "first")

    def test_bare_when_has_no_intent(self):
        self.assertEqual(ask.parse_question("when did I use supercrawl", NOW)["intent"],
                         "all")

    def test_last_week_is_a_window_not_an_intent(self):
        p = ask.parse_question("what did I do last week", NOW)
        self.assertEqual(p["window_label"], "last week")
        self.assertEqual(p["intent"], "all")
        # "last" was consumed by the window, so it must not become an intent.
        self.assertIsNotNone(p["window"])

    def test_yesterday_window(self):
        p = ask.parse_question("what was I doing yesterday", NOW)
        self.assertEqual(p["window_label"], "yesterday")
        self.assertEqual(p["window"][0], ts(2026, 9, 25, 0, 0))
        self.assertEqual(p["window"][1], ts(2026, 9, 26, 0, 0))

    def test_n_days_ago_window(self):
        p = ask.parse_question("what was I playing 3 days ago", NOW)
        self.assertEqual(p["window_label"], "3 days ago")
        self.assertEqual(p["window"][0], ts(2026, 9, 23, 0, 0))

    def test_weekday_resolves_to_the_most_recent_past_one(self):
        # NOW is Sat 26 Sep 2026; the previous Friday is the 25th.
        p = ask.parse_question("what did I do on friday", NOW)
        self.assertEqual(p["window_label"], "friday")
        self.assertEqual(p["window"][0], ts(2026, 9, 25, 0, 0))

    def test_subjectless_question_has_no_terms(self):
        # Drives the digest mode: nothing to rank, so lay out the window.
        p = ask.parse_question("what was I doing yesterday", NOW)
        self.assertEqual(p["terms"], [])

    def test_quoted_phrase_is_kept_and_ranked(self):
        p = ask.parse_question('what about "Hermes Agent dashboard"', NOW)
        self.assertIn("Hermes Agent dashboard", p["phrases"])

    def test_generic_verbs_are_not_retrieval_anchors(self):
        p = ask.parse_question("when did I play osu", NOW)
        # "play" is kept for scoring but is excluded from the anchor set, which
        # is what stops every played video/game from matching the question.
        self.assertIn("play", p["terms"])
        self.assertIn("play", ask._GENERIC_VERBS)
        anchors = [t for t in p["terms"] if t not in ask._GENERIC_VERBS]
        self.assertEqual(anchors, ["osu"])


class TestSearch(AskIndexCase):

    def _search(self, q, **kw):
        kw.setdefault("use_ai", False)
        kw.setdefault("now", NOW)
        return ask.search(q, **kw)

    def test_last_time_returns_the_most_recent_sitting(self):
        r = self._search("when's the last time I played osu")
        self.assertTrue(r["hits"])
        top = r["hits"][0]
        # The 2026-09-21 session runs to 01:00 on the 22nd, so it is the latest.
        self.assertEqual(top["day"], "2026-09-21")
        self.assertEqual(top["ts"], ts(2026, 9, 22, 1, 0))

    def test_first_time_returns_the_earliest_sitting(self):
        r = self._search("when did I first play osu")
        # The Steam library page on the 5th is the earliest osu *mention*.
        self.assertEqual(r["hits"][0]["day"], "2026-09-05")

    def test_generic_verb_alone_does_not_match_everything(self):
        # Every corpus document contains a form of "play" except the coding one;
        # an anchor-less match would drag in unrelated moments.
        r = self._search("when did I play")
        for h in r["hits"]:
            self.assertNotEqual(h["day"], "2026-09-20")

    def test_hits_carry_a_working_deep_link(self):
        r = self._search("when did I use supercrawl")
        h = r["hits"][0]
        self.assertEqual(h["link"], "/?day=%s&ts=%d" % (h["day"], h["ts"]))
        self.assertEqual(h["day"], "2026-09-20")

    def test_consecutive_moments_merge_into_one_sitting(self):
        r = self._search("when did I use supercrawl")
        top = r["hits"][0]
        # 21:00 and 21:20 are 20 minutes apart, so they are one sitting, and the
        # 21:00-21:30 schedule block covering the same stretch is folded in.
        self.assertEqual(top["day"], "2026-09-20")
        self.assertEqual(top["moments"], 2)
        self.assertEqual(top["ts"], ts(2026, 9, 20, 21, 0))
        self.assertEqual(top["end_ts"], ts(2026, 9, 20, 21, 30))

    def test_group_moments_merges_nearby_and_splits_far_apart(self):
        base = ts(2026, 9, 20, 19, 0)
        docs = [
            {"kind": "moment", "day": "2026-09-20", "ts": base, "score": 1.0, "head": "a"},
            {"kind": "moment", "day": "2026-09-20", "ts": base + 1200, "score": 1.0, "head": "b"},
            {"kind": "moment", "day": "2026-09-20", "ts": base + 3 * 3600, "score": 1.0, "head": "c"},
        ]
        groups = ask.group_moments(docs, gap_minutes=45)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["moments"], 2)
        self.assertEqual(groups[0]["end_ts"], base + 1200)

    def test_group_moments_never_merges_across_sessions(self):
        a = ts(2026, 9, 20, 23, 50)
        docs = [
            {"kind": "moment", "day": "2026-09-20", "ts": a, "score": 1.0, "head": "a"},
            {"kind": "moment", "day": "2026-09-21", "ts": a + 600, "score": 1.0, "head": "b"},
        ]
        self.assertEqual(len(ask.group_moments(docs, gap_minutes=45)), 2)

    def test_window_is_applied(self):
        # "yesterday" relative to the 21st is the 20th, which is the only
        # session in the fixture with that date.
        r = self._search(
            "what was I doing yesterday",
            now=datetime.datetime(2026, 9, 21, 12, 0),
        )
        self.assertTrue(r.get("digest"))
        self.assertEqual(r["window_label"], "yesterday")
        self.assertTrue(r["hits"])
        for h in r["hits"]:
            self.assertEqual(h["day"], "2026-09-20")

    def test_window_falls_back_when_nothing_is_inside_it(self):
        # The fixture has no osu on the 25th, so the answer must say so rather
        # than silently returning an unrelated day.
        r = self._search("when did I play osu yesterday", now=NOW)
        self.assertTrue(r["window_fallback"])
        self.assertIn("yesterday", r["answer"])

    def test_no_match_returns_an_explanation(self):
        r = self._search("when did I ride a helicopter")
        self.assertEqual(r["hits"], [])
        self.assertIn("could not find", r["answer"].lower())

    def test_empty_question_is_safe(self):
        r = self._search("   ")
        self.assertEqual(r["hits"], [])

    def test_digest_mode_for_a_subjectless_question(self):
        r = self._search("what was I doing yesterday", now=NOW)
        # The fixture has nothing on the 25th, so it falls back to all time and
        # must say so rather than inventing an empty answer.
        self.assertTrue(r.get("digest"))
        self.assertEqual(r["window_label"], "all time")
        self.assertTrue(r["hits"])

    def test_digest_mode_does_not_collapse_a_day_into_one_hit(self):
        # A whole day of 20-minute samples is one unbroken run of moments; a
        # digest must still list them individually rather than emit a single
        # 8-hour hit that says nothing.
        r = self._search(
            "what was I doing yesterday", now=datetime.datetime(2026, 9, 21, 12, 0)
        )
        self.assertTrue(r.get("digest"))
        self.assertGreater(len(r["hits"]), 1)

    def test_answer_mentions_the_time_it_found(self):
        r = self._search("when's the last time I played osu")
        self.assertIn("22 Sep 2026", r["answer"])

    def test_results_are_json_serialisable(self):
        r = self._search("when did I use supercrawl")
        json.dumps(r)  # must not raise


class TestRerankParsing(unittest.TestCase):
    """The model's reply is parsed defensively; a malformed reply must not crash."""

    def _rerank(self, reply, **kw):
        calls = {}

        def fake_chat(model, messages, **kwargs):
            calls["messages"] = messages
            return reply

        orig = ask._ollama_chat
        ask._ollama_chat = fake_chat
        try:
            hits = kw.pop("hits", [
                {"id": "m:1", "span": "Mon 1 Jan 2024, 10:00 - 10:20", "snippet": "alpha"},
                {"id": "m:2", "span": "Tue 2 Jan 2024, 10:00 - 10:20", "snippet": "beta"},
            ])
            parsed = {"intent": "last", "window_label": None}
            return ask._rerank("q?", hits, parsed)
        finally:
            ask._ollama_chat = orig

    def test_picks_the_chosen_candidate(self):
        v = self._rerank("ANSWER: Tuesday.\nBEST: 2\nOTHERS: 1")
        self.assertEqual(v["best"], 2)
        self.assertEqual(v["others"], [1])
        self.assertEqual(v["answer"], "Tuesday.")

    def test_out_of_range_choice_is_rejected(self):
        v = self._rerank("ANSWER: x\nBEST: 9\nOTHERS: none")
        self.assertIn("note", v)
        self.assertNotIn("best", v)

    def test_unparseable_reply_is_reported(self):
        v = self._rerank("I am not sure what to pick.")
        self.assertIn("note", v)

    def test_model_unavailable_is_reported_not_raised(self):
        def boom(model, messages, **kwargs):
            raise OSError("connection refused")

        orig = ask._ollama_chat
        ask._ollama_chat = boom
        try:
            v = ask._rerank("q?", [{"span": "s", "snippet": "x"}], {"intent": "all"})
        finally:
            ask._ollama_chat = orig
        self.assertIn("unavailable", v["note"])


if __name__ == "__main__":
    unittest.main()

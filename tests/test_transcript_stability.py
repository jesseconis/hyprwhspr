import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from mic_osd.transcript_stability import (
    TranscriptChangeTracker,
    decode_meta,
    encode_meta,
    meta_path_for,
)


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def _by_text(cues):
    return {cue.text: cue for cue in cues}


class TranscriptChangeTrackerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.tracker = TranscriptChangeTracker(highlight_seconds=1.8, clock=self.clock)

    def test_appended_words_are_never_highlighted(self):
        self.tracker.update("one two three four five six seven eight nine ten eleven twelve")
        self.tracker.update(
            "one two three four five six seven eight nine ten eleven twelve thirteen"
        )
        self.assertTrue(all(cue.highlight == 0.0 for cue in self.tracker.cues()))

    def test_revision_at_cursor_stays_quiet(self):
        self.tracker.update("alpha beta gamma delta epsilon zeta eta theta iota kappa trans")
        self.tracker.update(
            "alpha beta gamma delta epsilon zeta eta theta iota kappa transcript"
        )
        self.assertEqual(_by_text(self.tracker.cues())["transcript"].highlight, 0.0)

    def test_distant_revision_is_highlighted_then_fades(self):
        words = [f"w{i}" for i in range(20)]
        self.tracker.update(" ".join(words))
        revised = list(words)
        revised[2] = "REVISED"
        self.tracker.update(" ".join(revised))

        cues = _by_text(self.tracker.cues())
        self.assertEqual(cues["REVISED"].highlight, 1.0)
        # Untouched neighbours are not highlighted.
        self.assertEqual(cues["w1"].highlight, 0.0)
        self.assertEqual(cues["w3"].highlight, 0.0)

        self.clock.now += 1.0
        middle = _by_text(self.tracker.cues())["REVISED"].highlight
        self.assertTrue(0.0 < middle < 1.0)

        self.clock.now += 1.0
        self.assertEqual(_by_text(self.tracker.cues())["REVISED"].highlight, 0.0)

    def test_highlight_survives_later_appends(self):
        words = [f"w{i}" for i in range(20)]
        self.tracker.update(" ".join(words))
        revised = list(words)
        revised[2] = "REVISED"
        self.tracker.update(" ".join(revised))
        self.clock.now += 0.1
        self.tracker.update(" ".join(revised + ["more"]))
        self.assertEqual(_by_text(self.tracker.cues())["REVISED"].highlight, 1.0)

    def test_highlight_strength_grows_with_distance_from_end(self):
        words = [f"w{i}" for i in range(20)]
        self.tracker.update(" ".join(words))
        revised = list(words)
        revised[15] = "NEARISH"  # 4 words from the end
        revised[1] = "FAR"
        self.tracker.update(" ".join(revised))
        cues = _by_text(self.tracker.cues())
        self.assertGreater(cues["FAR"].highlight, cues["NEARISH"].highlight)
        self.assertGreater(cues["NEARISH"].highlight, 0.0)

    def test_committed_boundary_marks_final_words_and_settles(self):
        text = "hello world next seg"
        self.tracker.update(text, committed_length=len("hello world"))
        cues = _by_text(self.tracker.cues())
        self.assertEqual(cues["next"].settled, 0.0)
        self.assertEqual(cues["seg"].settled, 0.0)

        self.clock.now += 1.0
        cues = _by_text(self.tracker.cues())
        self.assertEqual(cues["hello"].settled, 1.0)
        self.assertEqual(cues["next"].settled, 0.0)

        # Final arrives for the second segment: words ease to the final style.
        self.tracker.update("hello world next segment", committed_length=24)
        cues = _by_text(self.tracker.cues())
        self.assertEqual(cues["next"].settled, 0.0)
        self.clock.now += self.tracker.SETTLE_SECONDS / 2
        self.assertTrue(0.0 < _by_text(self.tracker.cues())["next"].settled < 1.0)
        self.clock.now += self.tracker.SETTLE_SECONDS
        self.assertEqual(_by_text(self.tracker.cues())["segment"].settled, 1.0)

    def test_missing_boundary_after_known_keeps_committed_words(self):
        self.tracker.update("hello world tail", committed_length=11)
        self.clock.now += 1.0
        self.tracker.update("hello world tail more", committed_length=None)
        cues = _by_text(self.tracker.cues())
        self.assertEqual(cues["hello"].settled, 1.0)
        self.assertEqual(cues["more"].settled, 0.0)

    def test_without_boundary_words_settle_after_they_stop_changing(self):
        self.tracker.update("steady words")
        self.assertEqual(self.tracker.cues()[0].settled, 0.0)
        self.clock.now += 5.0
        self.assertEqual(self.tracker.cues()[0].settled, 1.0)

    def test_disabled_tracker_renders_everything_final(self):
        self.tracker.configure(1.8, enabled=False)
        words = [f"w{i}" for i in range(20)]
        self.tracker.update(" ".join(words), committed_length=0)
        revised = list(words)
        revised[2] = "REVISED"
        self.tracker.update(" ".join(revised), committed_length=0)
        for cue in self.tracker.cues():
            self.assertEqual((cue.settled, cue.highlight), (1.0, 0.0))

    def test_empty_text_resets(self):
        self.tracker.update("something", committed_length=9)
        self.tracker.update("")
        self.assertEqual(self.tracker.cues(), ())
        self.assertFalse(self.tracker.has_boundary)

    def test_paragraph_breaks_are_recorded(self):
        self.tracker.update("one\ntwo\n\nthree")
        breaks = [cue.breaks_before for cue in self.tracker.cues()]
        self.assertEqual(breaks, [0, 1, 2])

    def test_large_rewrite_falls_back_without_quadratic_blowup(self):
        self.tracker.MAX_DIFF_CELLS = 10
        old = " ".join(f"a{i}" for i in range(50))
        new = " ".join(f"b{i}" for i in range(50))
        self.tracker.update(old)
        self.tracker.update(new)
        self.assertEqual(len(self.tracker.cues()), 50)


class PreviewMetaTests(unittest.TestCase):
    def test_round_trip(self):
        text = "café 東京 ok"
        self.assertEqual(decode_meta(text, encode_meta(text, 4)), 4)

    def test_rejects_meta_for_different_text(self):
        raw = encode_meta("old text", 3)
        self.assertIsNone(decode_meta("new text", raw))

    def test_rejects_garbage(self):
        self.assertIsNone(decode_meta("x", "not json"))
        self.assertIsNone(decode_meta("x", None))
        self.assertIsNone(decode_meta("x", '{"v": 2}'))

    def test_meta_path_is_sidecar(self):
        path = Path("/run/user/1/hyprwhspr/transcript_preview")
        self.assertEqual(meta_path_for(path).name, "transcript_preview_meta")
        self.assertEqual(meta_path_for(path).parent, path.parent)


class PreviewFramePollTests(unittest.TestCase):
    def setUp(self):
        try:
            import mic_osd.main as main_module
        except Exception as exc:  # GTK unavailable in this environment
            self.skipTest(f"mic_osd.main unavailable: {exc}")
        self.main_module = main_module

    def test_frame_poll_reads_only_when_preview_file_is_replaced(self):
        import tempfile
        from unittest import mock

        main_module = self.main_module
        with tempfile.TemporaryDirectory() as tmp:
            preview = Path(tmp) / "transcript_preview"
            with mock.patch.object(main_module, "TRANSCRIPT_PREVIEW_FILE", preview):
                osd = object.__new__(main_module.MicOSD)
                osd.window = mock.Mock()
                osd._last_preview_text = None
                osd._last_preview_signature = None

                osd._poll_preview_file()
                osd.window.set_preview_text.assert_called_once_with("", committed_length=None)

                text = "hello world"
                preview.write_text(text, encoding="utf-8")
                meta_path_for(preview).write_text(encode_meta(text, 5), encoding="utf-8")
                osd._poll_preview_file()
                osd.window.set_preview_text.assert_called_with(text, committed_length=5)

                calls = osd.window.set_preview_text.call_count
                with mock.patch.object(Path, "read_text", side_effect=AssertionError):
                    osd._poll_preview_file()  # unchanged: stat only, no read
                self.assertEqual(osd.window.set_preview_text.call_count, calls)


if __name__ == "__main__":
    unittest.main()

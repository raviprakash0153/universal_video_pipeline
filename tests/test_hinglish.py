import tempfile
import unittest
from pathlib import Path

from hinglish import caption_words, to_hinglish
import video_pipeline
import clip_pipeline
import process_raj_podcast
from types import SimpleNamespace


class HinglishCaptionTests(unittest.TestCase):
    def test_common_spellings_and_mixed_script_preservation(self):
        self.assertEqual(to_hinglish("भाई, क्या clutch? नहीं! है बहुत OP"),
                         "Bhai, Kya clutch? Nahi! Hai Bahut OP")

    def test_exact_requested_combined_example(self):
        self.assertEqual(to_hinglish("भाई, क्या clutch कर दिया!"),
                         "Bhai, Kya clutch kar diya!")

    def test_common_phrases_conjuncts_and_schwa(self):
        self.assertEqual(to_hinglish("बहुत बढ़िया, शक्ति और कमल"),
                         "Bahut badhiya, shakti aur kamal")
        self.assertEqual(to_hinglish("मित्र"), "mitr")

    def test_nukta_and_combining_mark_variants(self):
        self.assertEqual(to_hinglish("ज़िंदगी ज़िंदगी"), "zindagi zindagi")
        self.assertEqual(to_hinglish("ड़"), "r")

    def test_gaming_numbers_punctuation_and_english_are_preserved(self):
        self.assertEqual(to_hinglish("भाई 1v3 clutch कर दिया! GG, 100%"),
                         "Bhai 1v3 clutch kar diya! GG, 100%")

    def test_caption_conversion_keeps_original_transcript_and_word_timing(self):
        original = [(1.125, 1.375, "भाई"), (1.4, 1.8, "clutch!"), (2.0, 2.25, "क्या")]
        display = caption_words(original, "hinglish")
        self.assertEqual(original, [(1.125, 1.375, "भाई"), (1.4, 1.8, "clutch!"), (2.0, 2.25, "क्या")])
        self.assertEqual(display, [(1.125, 1.375, "Bhai"), (1.4, 1.8, "clutch!"), (2.0, 2.25, "Kya")])

    def test_karaoke_events_use_transliterated_words_with_original_times(self):
        original = [(1.125, 1.375, "भाई"), (1.4, 1.8, "क्या")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "captions.ass"
            video_pipeline.write_ass(original, path, 1080, 1920, 64, 1370, "hinglish")
            ass = path.read_text(encoding="utf-8")
        self.assertIn("0:00:01.12,0:00:01.38", ass)
        self.assertIn("0:00:01.40,0:00:01.80", ass)
        self.assertIn("Bhai", ass)
        self.assertIn("Kya", ass)
        self.assertEqual(original[0], (1.125, 1.375, "भाई"))

    def test_original_language_is_unchanged_and_invalid_language_rejected(self):
        words = [(0, 1, "नहीं")]
        self.assertEqual(caption_words(words), words)
        with self.assertRaises(ValueError):
            caption_words(words, "fr")

    def test_original_order_timestamps_and_text_survive_both_modes(self):
        words = [(2.0, 2.3, "बहुत"), (1.0, 1.2, "भाई")]
        self.assertEqual(caption_words(words, "original"), words)
        shown = caption_words(words, "hinglish")
        self.assertEqual([(s, e) for s, e, _ in shown], [(2.0, 2.3), (1.0, 1.2)])
        self.assertEqual([t for _, _, t in shown], ["Bahut", "Bhai"])
        self.assertEqual([t for _, _, t in words], ["बहुत", "भाई"])

    def test_caption_language_options_exist_with_original_defaults(self):
        import inspect
        for module in (video_pipeline, clip_pipeline, process_raj_podcast):
            with self.subTest(module=module.__name__):
                source = inspect.getsource(module.main)
                self.assertIn('"--caption-language"', source)
                self.assertIn('choices=["original", "hinglish"]', source)
                self.assertIn('default="original"', source)

    def test_other_pipeline_renderers_use_same_hinglish_without_retiming(self):
        for module, args in ((clip_pipeline, ("Viral",)), (process_raj_podcast, ())):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "captions.ass"
                words = [SimpleNamespace(word=" भाई!", start=3.125, end=3.5),
                         SimpleNamespace(word=" clutch", start=3.5, end=4.0)]
                module.generate_ass_subtitles(words, str(path), *args, caption_language="hinglish")
                content = path.read_text(encoding="utf-8")
                self.assertIn("0:00:03.12,0:00:03.50", content)
                self.assertIn("Bhai!", content)
                self.assertIn("clutch", content)
                self.assertEqual(words[0].word, " भाई!")


if __name__ == "__main__":
    unittest.main()

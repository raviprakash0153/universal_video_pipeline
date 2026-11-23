import json
import unittest

from viral_moment_analyzer import (AnalyzerConfig, analyze_transcript, detect_audio_spikes,
                                   report, normalize_words, transcription_quality_issues)


def transcript(text="Oh no! clutch! Let's go!", start=20):
    return [{"start": start + i * .35, "end": start + i * .35 + .25, "text": word}
            for i, word in enumerate(text.split())]


class ViralMomentAnalyzerTests(unittest.TestCase):
    def test_malformed_word_timestamps_are_rejected(self):
        for item in ((float("nan"), 1, "bad"), (2, 1, "backwards"), (-1, 1, "negative"),
                     (1, 1, "zero duration"), (0, float("inf"), "infinite")):
            with self.subTest(item=item), self.assertRaises(ValueError):
                normalize_words([item])

    def test_long_duration_token_is_flagged_without_rewriting(self):
        words = [(1, 11, "БОЛЬШЕ")]
        issues = transcription_quality_issues(words)
        self.assertTrue(any("spans 10.00s" in issue for issue in issues))
        self.assertTrue(any("Cyrillic" in issue for issue in issues))
        self.assertEqual(normalize_words(words)[0]["text"], "БОЛЬШЕ")

    def test_valid_hindi_hinglish_and_english_tokens_are_retained(self):
        words = [(0, .3, "भाई"), (.4, .7, "clutch"), (.8, 1.1, "जीत"), (1.2, 1.4, "GG!")]
        self.assertEqual([w["text"] for w in normalize_words(words)], [w[2] for w in words])
        self.assertEqual(transcription_quality_issues(words), [])

    def test_alternating_hindi_english_code_switching_is_usable(self):
        words = [(20, 20.2, "भाई"), (20.3, 20.5, "that"), (20.6, 20.8, "clutch"),
                 (20.9, 21.1, "कर"), (21.2, 21.4, "दिया"), (21.5, 21.7, "lets"),
                 (21.8, 22, "go")]
        self.assertEqual(transcription_quality_issues(words), [])
        result = analyze_transcript(words, duration=100)
        self.assertTrue(result)
        self.assertGreater(result[0].transcript_score, 0)

    def test_suspicious_token_does_not_score_but_marks_overlapping_candidate(self):
        # Cyrillic lookalikes in a long token are retained, reported, and excluded from scoring.
        words = [(20, 30, "wowБОЛЬШЕ"), (31, 31.2, "ordinary"), (32, 32.2, "clutch")]
        candidate = analyze_transcript(words, duration=100)[0]
        baseline = analyze_transcript([(31, 31.2, "ordinary"), (32, 32.2, "clutch")], duration=100)[0]
        self.assertEqual(candidate.transcript_score, baseline.transcript_score)
        self.assertEqual(candidate.overall_score, baseline.overall_score)
        self.assertEqual(candidate.confidence, "review")
        self.assertTrue(candidate.quality_issues)
        result = report(words, duration=100)
        self.assertEqual(result["transcription_quality_issues"][0]["text"], "wowБОЛЬШЕ")
        self.assertTrue(result["candidates"][0]["quality_issues"])

    def test_long_span_cue_cannot_inflate_score(self):
        words = [(20, 40, "clutch")]
        self.assertEqual(analyze_transcript(words, duration=100), [])
        self.assertTrue(report(words, duration=100)["transcription_quality_issues"])

    def test_transcript_context_detects_english_hinglish_and_hindi(self):
        for words in (transcript("No way, that clutch was insane!"),
                      transcript("Bhai kya clutch kar diya, lets go!"),
                      transcript("अरे वाह, clutch कर दिया, जीत गए!")):
            with self.subTest(words=words):
                result = analyze_transcript(words, duration=100)
                self.assertTrue(result)
                self.assertGreater(result[0].transcript_score, 0)

    def test_audio_spike_detection_and_audio_evidence(self):
        windows = [{"start": i, "end": i + 1, "rms": 0.1} for i in range(6)]
        windows.append({"start": 21, "end": 22, "rms": 0.8, "shouting": True})
        spikes = detect_audio_spikes(windows)
        self.assertEqual(len(spikes), 1)
        result = analyze_transcript(transcript(), duration=100, audio_windows=windows)
        self.assertIn("unusually high audio energy", result[0].audio_evidence)

    def test_missing_engagement_and_audio_are_safe(self):
        result = analyze_transcript(transcript(), duration=100)
        self.assertEqual(result[0].engagement_score, 0)
        self.assertEqual(result[0].engagement_evidence, [])
        self.assertEqual(result[0].audio_evidence, [])

    def test_separate_moments_duration_bounds_and_ranking(self):
        words = transcript("wow clutch lets go", 20) + transcript("no way crazy", 70)
        ranked = analyze_transcript(words, duration=120,
                                    config=AnalyzerConfig(clip_duration=18, min_duration=10,
                                                          max_duration=20, min_separation=10))
        self.assertEqual(len(ranked), 2)
        self.assertTrue(all(10 <= c.duration <= 20 for c in ranked))
        self.assertGreaterEqual(abs(ranked[0].start - ranked[1].start), 10)
        self.assertGreaterEqual(ranked[0].overall_score, ranked[1].overall_score)

    def test_two_overlapping_candidates_merge_ranges_and_evidence(self):
        words = transcript("wow", 20) + transcript("clutch", 21)
        audio = [{"start": 17, "end": 27, "shouting": True}]
        result = analyze_transcript(words, duration=100, audio_windows=audio,
                                    engagement={"view_count": 100, "like_count": 10},
                                    config=AnalyzerConfig(clip_duration=10, min_duration=4,
                                                          max_duration=20))
        self.assertEqual(len(result), 1)
        candidate = result[0]
        self.assertEqual((candidate.start, candidate.end, candidate.duration), (15.125, 26.125, 11.0))
        self.assertEqual(candidate.transcript_evidence,
                         list(dict.fromkeys(candidate.transcript_evidence)))
        self.assertEqual(candidate.audio_evidence.count("shouting or excited speech annotated"), 1)
        self.assertEqual(candidate.engagement_score, 0)

    def test_multiple_overlapping_candidates_merge_all_evidence_once(self):
        words = transcript("wow", 20) + transcript("clutch", 21) + transcript("lets go", 22)
        result = analyze_transcript(words, duration=100,
                                    config=AnalyzerConfig(clip_duration=10, min_duration=4,
                                                          max_duration=20))
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0].start, result[0].end, result[0].duration), (15.125, 27.475, 12.35))
        self.assertEqual(len(result[0].transcript_evidence), len(set(result[0].transcript_evidence)))
        self.assertTrue(any("wow" in evidence for evidence in result[0].transcript_evidence))
        self.assertTrue(any("clutch" in evidence for evidence in result[0].transcript_evidence))
        self.assertTrue(any("lets go" in evidence for evidence in result[0].transcript_evidence))

    def test_non_overlapping_candidates_remain_separate(self):
        result = analyze_transcript(transcript("wow", 20) + transcript("clutch", 60), duration=100,
                                    config=AnalyzerConfig(clip_duration=10, min_duration=4,
                                                          max_duration=20))
        self.assertEqual(len(result), 2)
        self.assertLess(result[0].end, result[1].start)

    def test_merged_window_respects_video_boundaries_and_duration_cap(self):
        result = analyze_transcript(transcript("wow", 0) + transcript("clutch", 1), duration=8,
                                    config=AnalyzerConfig(clip_duration=10, min_duration=4,
                                                          max_duration=6))
        self.assertEqual(len(result), 1)
        self.assertGreaterEqual(result[0].start, 0)
        self.assertLessEqual(result[0].end, 8)
        self.assertLessEqual(result[0].duration, 6)
        self.assertEqual(result[0].duration, round(result[0].end - result[0].start, 3))

    def test_invalid_timestamps_empty_transcript_and_json_serialization(self):
        with self.assertRaises(ValueError):
            analyze_transcript([{"start": -1, "end": 2, "text": "wow"}])
        with self.assertRaises(ValueError):
            analyze_transcript([{"start": 3, "end": 2, "text": "wow"}])
        self.assertEqual(analyze_transcript([]), [])
        encoded = json.dumps(report(transcript(), duration=100), ensure_ascii=False)
        self.assertIn("moment-001", encoded)

    def test_supplied_public_engagement_only(self):
        candidate = analyze_transcript(transcript(), duration=100,
                                       engagement={"view_count": 1000, "like_count": 50})[0]
        self.assertEqual(candidate.engagement_score, 0)
        self.assertEqual(candidate.engagement_evidence, [])

    def test_substring_false_positives_do_not_score(self):
        result = analyze_transcript(transcript("wowza the enemyship was amazingness", 20), duration=100)
        self.assertEqual(result, [])

    def test_malformed_chapters_are_skipped_and_valid_chapter_is_local_evidence(self):
        metadata = {"chapters": [None, {}, {"start_time": "bad", "end_time": 40},
                                 {"start_time": 20, "end_time": 30}]}
        candidate = analyze_transcript(transcript(), duration=100, engagement=metadata)[0]
        self.assertEqual(candidate.engagement_score, .15)
        self.assertIn("overlaps a publicly listed chapter", candidate.engagement_evidence)

    def test_invalid_chapter_ranges_do_not_score_or_crash(self):
        metadata = {"chapters": [{"start_time": 30, "end_time": 20},
                                 {"start_time": None, "end_time": 50}]}
        candidate = analyze_transcript(transcript(), duration=100, engagement=metadata)[0]
        self.assertEqual(candidate.engagement_score, 0)
        self.assertEqual(candidate.engagement_evidence, [])

    def test_practical_gaming_cues_and_transcript_only_ranking(self):
        for phrase in ("Bhai one tap, squad wipe, let's go", "अरे भाई clutch कर दिया",
                       "Knocked him, revive karo, what a shot"):
            with self.subTest(phrase=phrase):
                result = analyze_transcript(transcript(phrase, 20), duration=100)
                self.assertTrue(result)
                self.assertGreater(result[0].transcript_score, 0)
                self.assertEqual(result[0].audio_score, 0)
                self.assertEqual(result[0].engagement_score, 0)


if __name__ == "__main__":
    unittest.main()

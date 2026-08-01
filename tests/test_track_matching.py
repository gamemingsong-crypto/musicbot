import unittest

import main


class FakeSearchTrack:
    def __init__(self, title, author="The Artist", length=180_000):
        self.title = title
        self.author = author
        self.length = length


class TrackMatchingTests(unittest.TestCase):
    def test_original_audio_scores_above_clean_version(self):
        source = {
            "title": "Example Song",
            "artist": "The Artist",
            "duration": 180,
        }
        original = FakeSearchTrack("Example Song (Official Audio)")
        clean = FakeSearchTrack("Example Song (Clean Version)")

        self.assertGreater(
            main.score_youtube_track(original, source),
            main.score_youtube_track(clean, source),
        )

    def test_explicit_and_uncensored_versions_receive_a_bonus(self):
        source = {"title": "Example Song", "artist": "The Artist"}
        plain = FakeSearchTrack("Example Song")
        explicit = FakeSearchTrack("Example Song Explicit")
        uncensored = FakeSearchTrack("Example Song Uncensored")

        self.assertGreater(
            main.score_youtube_track(explicit, source),
            main.score_youtube_track(plain, source),
        )
        self.assertGreater(
            main.score_youtube_track(uncensored, source),
            main.score_youtube_track(plain, source),
        )

    def test_a_song_actually_named_clean_is_not_penalized(self):
        source = {"title": "Clean", "artist": "The Artist"}
        candidate = FakeSearchTrack("Clean (Official Audio)")

        self.assertGreaterEqual(main.score_youtube_track(candidate, source), 100)

    def test_general_search_can_rank_uncensored_before_clean(self):
        tracks = [
            FakeSearchTrack("Example Song Clean"),
            FakeSearchTrack("Example Song"),
            FakeSearchTrack("Example Song Explicit"),
        ]
        ranked = sorted(
            tracks,
            key=lambda track: main.score_version_preference(
                track.title,
                "Example Song",
            ),
            reverse=True,
        )

        self.assertEqual(ranked[0].title, "Example Song Explicit")
        self.assertEqual(ranked[-1].title, "Example Song Clean")


if __name__ == "__main__":
    unittest.main()

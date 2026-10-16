import unittest

from webui.youtube import extract_youtube_video_id


class YouTubeVideoIdTests(unittest.TestCase):
    def test_accepts_video_id_and_supported_youtube_urls(self) -> None:
        inputs = [
            "dQw4w9WgXcQ",
            " https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=20 ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://youtube.com/shorts/dQw4w9WgXcQ",
            "https://youtube-nocookie.com/embed/dQw4w9WgXcQ",
        ]

        self.assertEqual(
            [extract_youtube_video_id(value) for value in inputs],
            ["dQw4w9WgXcQ"] * len(inputs),
        )

    def test_rejects_invalid_ids_and_non_youtube_urls(self) -> None:
        inputs = [
            "video-id",
            "https://evil.example/watch?v=dQw4w9WgXcQ",
            "https://youtube.com.evil.example/watch?v=dQw4w9WgXcQ",
            "javascript://youtube.com/watch?v=dQw4w9WgXcQ",
        ]
        for value in inputs:
            with self.subTest(value=value), self.assertRaises(ValueError):
                extract_youtube_video_id(value)


if __name__ == "__main__":
    unittest.main()

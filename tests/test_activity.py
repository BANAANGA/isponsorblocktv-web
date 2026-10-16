import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from iSponsorBlockTV.api_helpers import ApiHelper
from webui.activity import ActivityStore
from webui.runner import _interval_seconds, install_activity_hooks


class ActivityStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = ActivityStore(Path(self.temporary_directory.name) / "activity.sqlite3")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_history_groups_playback_updates_for_same_video(self) -> None:
        now = datetime(2026, 10, 16, 12, tzinfo=UTC)
        self.store.record_video("video-1", "Video", "Living room", 2, now=now)
        self.store.record_video(
            "video-1", "Video", "Living room", 3, now=now + timedelta(minutes=2)
        )

        history = self.store.history()

        self.assertEqual(history["total"], 1)
        self.assertEqual(history["items"][0]["segment_count"], 3)
        self.assertEqual(history["items"][0]["started_at"], now.isoformat())

    def test_history_records_a_new_session_after_a_gap(self) -> None:
        now = datetime(2026, 10, 16, 12, tzinfo=UTC)
        self.store.record_video("video-1", "Video", "Living room", 1, now=now)
        self.store.record_video(
            "video-1", "Video", "Living room", 1, now=now + timedelta(minutes=11)
        )

        self.assertEqual(self.store.history()["total"], 2)

    def test_stats_aggregate_total_category_device_and_recent_activity(self) -> None:
        now = datetime.now(UTC)
        self.store.record_video("video-1", "Video", "Living room", 2, now=now)
        self.store.record_skip(
            "video-1",
            "Living room",
            30,
            2,
            {"sponsor": 1, "intro": 1},
            {"sponsor": 20, "intro": 10},
            now=now,
        )

        stats = self.store.stats()

        self.assertEqual(stats["totals"]["videos_watched"], 1)
        self.assertEqual(stats["totals"]["skip_events"], 1)
        self.assertEqual(stats["totals"]["segments_skipped"], 2)
        self.assertEqual(stats["totals"]["seconds_saved"], 30)
        self.assertEqual(stats["last_30_days"]["skip_events"], 1)
        self.assertEqual(
            {item["category"]: item["segments_skipped"] for item in stats["by_category"]},
            {"intro": 1, "sponsor": 1},
        )
        self.assertEqual(stats["by_device"][0]["device"], "Living room")
        self.assertEqual(len(stats["daily"]), 1)

    def test_activity_persists_across_store_instances(self) -> None:
        self.store.record_video("video-1", "Video", "Living room", 1)
        reopened = ActivityStore(self.store.path)

        self.assertEqual(reopened.history()["total"], 1)


class SegmentIntervalTests(unittest.TestCase):
    def test_category_duration_unions_overlapping_segments(self) -> None:
        self.assertEqual(_interval_seconds([(0, 10), (5, 15), (20, 22)]), 17)

    def test_upstream_segment_processing_preserves_category_statistics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            install_activity_hooks(ActivityStore(Path(directory) / "activity.sqlite3"))
            segments, _ = ApiHelper.process_segments(
                {
                    "segments": [
                        {
                            "UUID": "uuid-1",
                            "category": "sponsor",
                            "segment": [10, 25],
                            "locked": 1,
                        },
                        {
                            "UUID": "uuid-2",
                            "category": "intro",
                            "segment": [24, 40],
                            "locked": 1,
                        },
                    ]
                },
                1,
            )

        self.assertEqual(len(segments), 1)
        self.assertEqual(set(segments[0]["UUID"]), {"uuid-1", "uuid-2"})
        self.assertEqual(segments[0]["category_counts"], {"sponsor": 1, "intro": 1})
        self.assertEqual(segments[0]["category_seconds"], {"sponsor": 15, "intro": 16})


if __name__ == "__main__":
    unittest.main()

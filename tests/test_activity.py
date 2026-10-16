import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from iSponsorBlockTV.api_helpers import ApiHelper
from webui.activity import ActivityStore
from webui.runner import _interval_seconds, _observed_watch_seconds, install_activity_hooks


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

    def test_history_limit_prunes_oldest_sessions_and_can_be_reduced(self) -> None:
        start = datetime(2026, 10, 16, 12, tzinfo=UTC)
        self.store.set_history_limit(2)
        for index in range(3):
            timestamp = start + timedelta(minutes=index * 11)
            self.store.record_video(
                f"video-{index}", f"Video {index}", "Living room", 0, now=timestamp
            )
            self.store.record_watch_time(
                f"video-{index}", "Living room", 10, now=timestamp
            )

        self.assertEqual(
            [video["video_id"] for video in self.store.history(limit=10)["items"]],
            ["video-2", "video-1"],
        )
        self.assertEqual(self.store.stats()["totals"]["watched_seconds"], 30)

        self.store.set_history_limit(1)
        self.assertEqual(self.store.history()["total"], 1)

    def test_disabling_video_history_keeps_aggregate_activity(self) -> None:
        store = ActivityStore(
            Path(self.temporary_directory.name) / "no-video-history.sqlite3",
            record_video_history=False,
        )
        now = datetime(2026, 10, 12, 8, 30, tzinfo=UTC)
        store.record_video("video-1", "Video", "Living room", 0, now=now)
        store.record_watch_time("video-1", "Living room", 90, now=now)
        store.record_skip(
            "video-1",
            "Living room",
            30,
            1,
            {"sponsor": 1},
            {"sponsor": 30},
            now=now,
        )

        stats = store.stats()

        self.assertEqual(store.history()["total"], 0)
        self.assertEqual(stats["totals"]["watched_seconds"], 90)
        self.assertEqual(stats["totals"]["skip_events"], 1)
        self.assertEqual(stats["by_hour"][8]["watched_seconds"], 90)
        self.assertEqual(stats["by_hour"][8]["seconds_saved"], 30)
        self.assertEqual(stats["by_weekday"][0]["watched_seconds"], 90)
        self.assertEqual(stats["by_weekday"][0]["seconds_saved"], 30)
        with store._connection() as connection:
            self.assertEqual(connection.execute("SELECT video_id FROM skip_events").fetchone()[0], "")

    def test_activity_time_breakdowns_use_utc_hour_and_monday_first_weekdays(self) -> None:
        monday = datetime(2026, 10, 12, 8, 15, tzinfo=UTC)
        tuesday = datetime(2026, 10, 13, 19, 15, tzinfo=UTC)
        self.store.record_watch_time("video-1", "Living room", 120, now=monday)
        self.store.record_watch_time("video-2", "Living room", 60, now=tuesday)
        self.store.record_skip(
            "video-1", "Living room", 45, 1, {"sponsor": 1}, {"sponsor": 45}, now=monday
        )
        self.store.record_skip(
            "video-2", "Living room", 90, 1, {"intro": 1}, {"intro": 90}, now=tuesday
        )

        stats = self.store.stats()

        self.assertEqual(len(stats["by_hour"]), 24)
        self.assertEqual(len(stats["by_weekday"]), 7)
        self.assertEqual(stats["by_hour"][8]["watched_seconds"], 120)
        self.assertEqual(stats["by_hour"][8]["seconds_saved"], 45)
        self.assertEqual(stats["by_hour"][19]["watched_seconds"], 60)
        self.assertEqual(stats["by_hour"][19]["seconds_saved"], 90)
        self.assertEqual(stats["by_weekday"][0]["watched_seconds"], 120)
        self.assertEqual(stats["by_weekday"][0]["seconds_saved"], 45)
        self.assertEqual(stats["by_weekday"][1]["watched_seconds"], 60)
        self.assertEqual(stats["by_weekday"][1]["seconds_saved"], 90)

    def test_clear_history_removes_video_and_activity_records(self) -> None:
        now = datetime.now(UTC)
        self.store.record_video("video-1", "Video", "Living room", 1, now=now)
        self.store.record_watch_time("video-1", "Living room", 60, now=now)
        self.store.record_skip(
            "video-1",
            "Living room",
            10,
            1,
            {"sponsor": 1},
            {"sponsor": 10},
            now=now,
        )
        self.store.set_now_playing("video-1", "Living room", playing=True, now=now)

        self.store.clear_history()

        self.assertEqual(self.store.history()["total"], 0)
        self.assertEqual(self.store.stats()["totals"]["skip_events"], 0)
        self.assertEqual(self.store.stats()["totals"]["watched_seconds"], 0)
        self.assertEqual(self.store.now_playing(), [])

    def test_history_search_filters_video_metadata_and_escapes_wildcards(self) -> None:
        self.store.record_video("video-1", "Red triangle", "Example channel", 1)
        self.store.record_video("video-2", "100% complete", "Other channel", 1)
        self.store.record_video("video-3", "Another video", "Kitchen TV", 1)

        self.assertEqual(self.store.history(search="example")["items"][0]["video_id"], "video-1")
        self.assertEqual(self.store.history(search="Kitchen")["items"][0]["video_id"], "video-3")
        self.assertEqual(self.store.history(search="%")["items"][0]["video_id"], "video-2")
        self.assertEqual(self.store.history(search="missing")["total"], 0)

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
        self.store.record_watch_time("video-1", "Living room", 120)

        stats = self.store.stats()

        self.assertEqual(stats["totals"]["videos_watched"], 1)
        self.assertEqual(stats["totals"]["skip_events"], 1)
        self.assertEqual(stats["totals"]["segments_skipped"], 2)
        self.assertEqual(stats["totals"]["seconds_saved"], 30)
        self.assertEqual(stats["totals"]["watched_seconds"], 120)
        self.assertEqual(stats["last_30_days"]["skip_events"], 1)
        self.assertEqual(stats["last_30_days"]["watched_seconds"], 120)
        self.assertEqual(
            {item["category"]: item["segments_skipped"] for item in stats["by_category"]},
            {"intro": 1, "sponsor": 1},
        )
        self.assertEqual(stats["by_device"][0]["device"], "Living room")
        self.assertEqual(len(stats["daily"]), 1)
        self.assertEqual(stats["daily"][0]["watched_seconds"], 120)
        self.assertEqual(stats["daily"][0]["seconds_saved"], 30)
        self.assertEqual(stats["top_videos"][0]["video_id"], "video-1")

    def test_activity_persists_across_store_instances(self) -> None:
        self.store.record_video("video-1", "Video", "Living room", 1)
        reopened = ActivityStore(self.store.path)

        self.assertEqual(reopened.history()["total"], 1)

    def test_video_history_stores_public_channel_and_thumbnail_metadata(self) -> None:
        self.store.record_video(
            "video-1",
            "Resolved video title",
            "Living room",
            2,
            channel="Example channel",
            channel_url="https://www.youtube.com/@example",
            thumbnail_url="https://i.ytimg.com/vi/video-1/hqdefault.jpg",
        )

        video = self.store.history()["items"][0]

        self.assertEqual(video["channel"], "Example channel")
        self.assertEqual(video["channel_url"], "https://www.youtube.com/@example")
        self.assertEqual(video["thumbnail_url"], "https://i.ytimg.com/vi/video-1/hqdefault.jpg")
        self.store.record_video("video-1", "video-1", "Living room", 2)
        self.assertEqual(self.store.history()["items"][0]["title"], "Resolved video title")

    def test_now_playing_tracks_active_devices_and_uses_video_metadata(self) -> None:
        now = datetime.now(UTC)
        self.store.record_video(
            "video-1",
            "Current video",
            "Living room",
            0,
            channel="Example channel",
            thumbnail_url="https://example.com/thumb.jpg",
            now=now,
        )
        self.store.set_now_playing("video-1", "Living room", playing=True, now=now)

        current = self.store.now_playing()

        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["title"], "Current video")
        self.assertEqual(current[0]["channel"], "Example channel")
        self.assertEqual(current[0]["thumbnail_url"], "https://example.com/thumb.jpg")

        self.store.set_now_playing("video-1", "Living room", playing=False)
        self.assertEqual(self.store.now_playing(), [])

    def test_now_playing_ignores_stale_playback_updates(self) -> None:
        stale = datetime.now(UTC) - timedelta(minutes=5)
        self.store.set_now_playing("video-1", "Living room", playing=True, now=stale)

        self.assertEqual(self.store.now_playing(), [])

    def test_existing_activity_database_migrates_video_metadata_columns(self) -> None:
        legacy_path = Path(self.temporary_directory.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as connection:
            connection.execute(
                """
                CREATE TABLE video_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    device TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    segment_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )

        migrated = ActivityStore(legacy_path)
        migrated.record_video("video-1", "Title", "Living room", 0)

        self.assertEqual(migrated.history()["items"][0]["channel"], "")
        with sqlite3.connect(legacy_path) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(video_history)")}
        self.assertIn("watched_seconds", columns)

    def test_activity_migration_backfills_daily_watch_totals(self) -> None:
        legacy_path = Path(self.temporary_directory.name) / "watched.sqlite3"
        with sqlite3.connect(legacy_path) as connection:
            connection.execute(
                """
                CREATE TABLE video_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT '',
                    channel_url TEXT NOT NULL DEFAULT '',
                    thumbnail_url TEXT NOT NULL DEFAULT '',
                    device TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    segment_count INTEGER NOT NULL DEFAULT 0,
                    watched_seconds REAL NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                """
                INSERT INTO video_history
                    (video_id, title, device, started_at, last_seen_at, watched_seconds)
                VALUES ('video-1', 'Video', 'Living room',
                    '2026-10-15T12:00:00+00:00', '2026-10-15T12:00:00+00:00', 45)
                """
            )

        migrated = ActivityStore(legacy_path)

        self.assertEqual(migrated.stats()["totals"]["watched_seconds"], 45)
        self.assertEqual(migrated.stats()["daily"][0]["watched_seconds"], 45)


class SegmentIntervalTests(unittest.TestCase):
    def test_watched_time_uses_only_forward_playback_position(self) -> None:
        playing = ("video-1", True, 10.0, 100.0)

        self.assertEqual(_observed_watch_seconds(playing, "video-1", 18.0, 108.0), 8.0)
        self.assertEqual(_observed_watch_seconds(playing, "video-1", 30.0, 108.0), 8.0)
        self.assertEqual(_observed_watch_seconds(playing, "video-1", 5.0, 108.0), 0.0)
        self.assertEqual(_observed_watch_seconds(playing, "video-2", 18.0, 108.0), 0.0)
        self.assertEqual(_observed_watch_seconds(("video-1", False, 10.0, 100.0), "video-1", 18.0, 108.0), 0.0)

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

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator


class ActivityStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS video_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT '',
                    channel_url TEXT NOT NULL DEFAULT '',
                    thumbnail_url TEXT NOT NULL DEFAULT '',
                    device TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    segment_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS video_history_started_at
                    ON video_history(started_at DESC);
                CREATE TABLE IF NOT EXISTS skip_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT NOT NULL,
                    device TEXT NOT NULL,
                    skipped_at TEXT NOT NULL,
                    seconds_saved REAL NOT NULL,
                    segment_count INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS skip_events_skipped_at
                    ON skip_events(skipped_at DESC);
                CREATE TABLE IF NOT EXISTS skip_categories (
                    skip_event_id INTEGER NOT NULL REFERENCES skip_events(id) ON DELETE CASCADE,
                    category TEXT NOT NULL,
                    segment_count INTEGER NOT NULL,
                    seconds_saved REAL NOT NULL,
                    PRIMARY KEY (skip_event_id, category)
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(video_history)").fetchall()
            }
            for name in ("channel", "channel_url", "thumbnail_url"):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE video_history ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                    )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def record_video(
        self,
        video_id: str,
        title: str,
        device: str,
        segment_count: int,
        channel: str = "",
        channel_url: str = "",
        thumbnail_url: str = "",
        *,
        now: datetime | None = None,
    ) -> None:
        timestamp = (now or datetime.now(UTC)).isoformat()
        with self._connection() as connection:
            recent = connection.execute(
                """
                SELECT id FROM video_history
                WHERE video_id = ? AND device = ? AND last_seen_at >= ?
                ORDER BY last_seen_at DESC LIMIT 1
                """,
                (video_id, device, (datetime.fromisoformat(timestamp) - timedelta(minutes=10)).isoformat()),
            ).fetchone()
            if recent:
                connection.execute(
                    """
                    UPDATE video_history
                    SET title = CASE WHEN channel != '' AND ? = video_id THEN title ELSE ? END,
                        channel = COALESCE(NULLIF(?, ''), channel),
                        channel_url = COALESCE(NULLIF(?, ''), channel_url),
                        thumbnail_url = COALESCE(NULLIF(?, ''), thumbnail_url),
                        last_seen_at = ?, segment_count = MAX(segment_count, ?)
                    WHERE id = ?
                    """,
                    (
                        title,
                        title,
                        channel,
                        channel_url,
                        thumbnail_url,
                        timestamp,
                        segment_count,
                        recent["id"],
                    ),
                )
                return
            connection.execute(
                """
                INSERT INTO video_history (
                    video_id, title, channel, channel_url, thumbnail_url, device,
                    started_at, last_seen_at, segment_count
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    video_id,
                    title,
                    channel,
                    channel_url,
                    thumbnail_url,
                    device,
                    timestamp,
                    timestamp,
                    segment_count,
                ),
            )

    def update_video_metadata(
        self,
        video_id: str,
        device: str,
        title: str,
        channel: str,
        channel_url: str,
        thumbnail_url: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE video_history SET title = ?, channel = ?, channel_url = ?,
                    thumbnail_url = ?
                WHERE id = (
                    SELECT id FROM video_history
                    WHERE video_id = ? AND device = ?
                    ORDER BY started_at DESC LIMIT 1
                )
                """,
                (title, channel, channel_url, thumbnail_url, video_id, device),
            )

    def update_video_segment_count(self, video_id: str, device: str, segment_count: int) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE video_history SET segment_count = MAX(segment_count, ?)
                WHERE id = (
                    SELECT id FROM video_history
                    WHERE video_id = ? AND device = ?
                    ORDER BY started_at DESC LIMIT 1
                )
                """,
                (max(0, segment_count), video_id, device),
            )

    def record_skip(
        self,
        video_id: str,
        device: str,
        seconds_saved: float,
        segment_count: int,
        category_counts: dict[str, int],
        category_seconds: dict[str, float],
        *,
        now: datetime | None = None,
    ) -> None:
        timestamp = (now or datetime.now(UTC)).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO skip_events (video_id, device, skipped_at, seconds_saved, segment_count)
                VALUES (?, ?, ?, ?, ?)
                """,
                (video_id, device, timestamp, max(0.0, seconds_saved), max(0, segment_count)),
            )
            connection.executemany(
                """
                INSERT INTO skip_categories (skip_event_id, category, segment_count, seconds_saved)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        cursor.lastrowid,
                        category,
                        max(0, count),
                        max(0.0, category_seconds.get(category, 0.0)),
                    )
                    for category, count in category_counts.items()
                ],
            )

    def history(self, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT video_id, title, channel, channel_url, thumbnail_url,
                    device, started_at, last_seen_at, segment_count
                FROM video_history ORDER BY started_at DESC LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
            total = connection.execute("SELECT COUNT(*) FROM video_history").fetchone()[0]
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def stats(self) -> dict[str, Any]:
        cutoff = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        with self._connect() as connection:
            totals = connection.execute(
                """
                SELECT COUNT(*) AS skip_events, COALESCE(SUM(segment_count), 0) AS segments_skipped,
                    COALESCE(SUM(seconds_saved), 0) AS seconds_saved
                FROM skip_events
                """
            ).fetchone()
            video_count = connection.execute("SELECT COUNT(*) FROM video_history").fetchone()[0]
            devices = connection.execute(
                """
                SELECT device, COUNT(*) AS skip_events, SUM(segment_count) AS segments_skipped,
                    SUM(seconds_saved) AS seconds_saved
                FROM skip_events GROUP BY device ORDER BY seconds_saved DESC
                """
            ).fetchall()
            categories = connection.execute(
                """
                SELECT category, SUM(segment_count) AS segments_skipped,
                    SUM(seconds_saved) AS seconds_saved, COUNT(*) AS skip_events
                FROM skip_categories GROUP BY category ORDER BY seconds_saved DESC
                """
            ).fetchall()
            daily = connection.execute(
                """
                SELECT substr(skipped_at, 1, 10) AS day, COUNT(*) AS skip_events,
                    SUM(segment_count) AS segments_skipped, SUM(seconds_saved) AS seconds_saved
                FROM skip_events WHERE skipped_at >= ?
                GROUP BY day ORDER BY day
                """,
                (cutoff,),
            ).fetchall()
            recent = connection.execute(
                """
                SELECT COUNT(*) AS skip_events, COALESCE(SUM(segment_count), 0) AS segments_skipped,
                    COALESCE(SUM(seconds_saved), 0) AS seconds_saved
                FROM skip_events WHERE skipped_at >= ?
                """,
                (cutoff,),
            ).fetchone()
            recent_videos = connection.execute(
                "SELECT COUNT(*) FROM video_history WHERE started_at >= ?", (cutoff,)
            ).fetchone()[0]
        return {
            "totals": {
                "videos_watched": video_count,
                "skip_events": totals["skip_events"],
                "segments_skipped": totals["segments_skipped"],
                "seconds_saved": round(totals["seconds_saved"], 1),
                "time_saved_days": round(totals["seconds_saved"] / 86400, 2),
            },
            "last_30_days": {
                "videos_watched": recent_videos,
                "skip_events": recent["skip_events"],
                "segments_skipped": recent["segments_skipped"],
                "seconds_saved": round(recent["seconds_saved"], 1),
            },
            "by_category": [dict(row) for row in categories],
            "by_device": [dict(row) for row in devices],
            "daily": [dict(row) for row in daily],
        }

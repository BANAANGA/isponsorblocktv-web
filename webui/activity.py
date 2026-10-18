import math
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo


class ActivityStore:
    def __init__(
        self,
        path: Path,
        max_history_videos: int = 1000,
        record_video_history: bool = True,
    ) -> None:
        if not 1 <= max_history_videos <= 10000:
            raise ValueError("Video history limit must be between 1 and 10000")
        self.path = path
        self.max_history_videos = max_history_videos
        self.record_video_history = record_video_history
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            had_daily_watch_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'watch_time_daily'"
            ).fetchone() is not None
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
                    segment_count INTEGER NOT NULL DEFAULT 0,
                    watched_seconds REAL NOT NULL DEFAULT 0,
                    duration_seconds REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS video_history_started_at
                    ON video_history(started_at DESC);
                CREATE TABLE IF NOT EXISTS watch_time_daily (
                    day TEXT PRIMARY KEY,
                    watched_seconds REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS watch_time_by_hour (
                    hour INTEGER PRIMARY KEY CHECK (hour BETWEEN 0 AND 23),
                    watched_seconds REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS watch_time_by_weekday (
                    weekday INTEGER PRIMARY KEY CHECK (weekday BETWEEN 0 AND 6),
                    watched_seconds REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS watch_time_buckets (
                    bucket_start TEXT PRIMARY KEY,
                    watched_seconds REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS now_playing (
                    device TEXT PRIMARY KEY,
                    screen_id TEXT NOT NULL DEFAULT '',
                    video_id TEXT NOT NULL,
                    is_playing INTEGER NOT NULL DEFAULT 1,
                    position_seconds REAL NOT NULL DEFAULT 0,
                    duration_seconds REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
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
            if "watched_seconds" not in columns:
                connection.execute(
                    "ALTER TABLE video_history ADD COLUMN watched_seconds REAL NOT NULL DEFAULT 0"
                )
            if "duration_seconds" not in columns:
                connection.execute(
                    "ALTER TABLE video_history ADD COLUMN duration_seconds REAL NOT NULL DEFAULT 0"
                )
            now_playing_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(now_playing)").fetchall()
            }
            for name, declaration in (
                ("screen_id", "TEXT NOT NULL DEFAULT ''"),
                ("is_playing", "INTEGER NOT NULL DEFAULT 1"),
                ("position_seconds", "REAL NOT NULL DEFAULT 0"),
                ("duration_seconds", "REAL NOT NULL DEFAULT 0"),
            ):
                if name not in now_playing_columns:
                    connection.execute(
                        f"ALTER TABLE now_playing ADD COLUMN {name} {declaration}"
                    )
            if not had_daily_watch_table:
                connection.execute(
                    """
                    INSERT INTO watch_time_daily (day, watched_seconds)
                    SELECT substr(started_at, 1, 10), SUM(watched_seconds)
                    FROM video_history
                    WHERE watched_seconds > 0
                    GROUP BY substr(started_at, 1, 10)
                    """
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
        duration_seconds: float = 0,
        *,
        now: datetime | None = None,
    ) -> None:
        if not self.record_video_history:
            return
        duration_seconds = (
            max(0.0, duration_seconds) if math.isfinite(duration_seconds) else 0.0
        )
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
                        last_seen_at = ?, segment_count = MAX(segment_count, ?),
                        duration_seconds = MAX(duration_seconds, ?)
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
                        duration_seconds,
                        recent["id"],
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO video_history (
                        video_id, title, channel, channel_url, thumbnail_url, device,
                        started_at, last_seen_at, segment_count, duration_seconds
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        duration_seconds,
                    ),
                )
            self._prune_history(connection)

    def set_history_limit(self, max_history_videos: int) -> None:
        if not 1 <= max_history_videos <= 10000:
            raise ValueError("Video history limit must be between 1 and 10000")
        self.max_history_videos = max_history_videos
        with self._connection() as connection:
            self._prune_history(connection)

    def _prune_history(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            DELETE FROM video_history
            WHERE id NOT IN (
                SELECT id FROM video_history
                ORDER BY started_at DESC, id DESC
                LIMIT ?
            )
            """,
            (self.max_history_videos,),
        )

    def clear_history(self) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM video_history")
            connection.execute("DELETE FROM now_playing")
            connection.execute("DELETE FROM skip_categories")
            connection.execute("DELETE FROM skip_events")
            connection.execute("DELETE FROM watch_time_daily")
            connection.execute("DELETE FROM watch_time_by_hour")
            connection.execute("DELETE FROM watch_time_by_weekday")
            connection.execute("DELETE FROM watch_time_buckets")

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

    def set_now_playing(
        self,
        video_id: str,
        device: str,
        *,
        playing: bool,
        active: bool | None = None,
        screen_id: str = "",
        position_seconds: float = 0,
        duration_seconds: float = 0,
        now: datetime | None = None,
    ) -> None:
        position_seconds = (
            max(0.0, position_seconds)
            if math.isfinite(position_seconds) and position_seconds > 0
            else 0.0
        )
        duration_seconds = (
            max(0.0, duration_seconds)
            if math.isfinite(duration_seconds) and duration_seconds > 0
            else 0.0
        )
        if active is None:
            active = playing
        with self._connection() as connection:
            if active and video_id:
                connection.execute(
                    """
                    INSERT INTO now_playing (
                        device, screen_id, video_id, is_playing, position_seconds,
                        duration_seconds, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(device) DO UPDATE SET
                        screen_id = excluded.screen_id,
                        video_id = excluded.video_id,
                        is_playing = excluded.is_playing,
                        position_seconds = excluded.position_seconds,
                        duration_seconds = excluded.duration_seconds,
                        updated_at = excluded.updated_at
                    """,
                    (
                        device,
                        screen_id,
                        video_id,
                        int(playing),
                        position_seconds,
                        duration_seconds,
                        (now or datetime.now(UTC)).isoformat(),
                    ),
                )
            else:
                connection.execute("DELETE FROM now_playing WHERE device = ?", (device,))

    def now_playing(self, *, max_age_seconds: int = 120) -> list[dict[str, Any]]:
        cutoff = (datetime.now(UTC) - timedelta(seconds=max_age_seconds)).isoformat()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT current.device, current.screen_id, current.video_id,
                    current.is_playing,
                    current.position_seconds, current.duration_seconds, current.updated_at,
                    COALESCE(history.title, current.video_id) AS title,
                    COALESCE(history.channel, '') AS channel,
                    COALESCE(history.channel_url, '') AS channel_url,
                    COALESCE(history.thumbnail_url, '') AS thumbnail_url
                FROM now_playing AS current
                LEFT JOIN video_history AS history
                    ON history.id = (
                        SELECT id FROM video_history
                        WHERE video_id = current.video_id AND device = current.device
                        ORDER BY started_at DESC LIMIT 1
                    )
                WHERE current.updated_at >= ? OR current.is_playing = 0
                ORDER BY current.device
                """,
                (cutoff,),
            ).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            duration = item["duration_seconds"]
            item["viewed_percent"] = (
                round(min(100.0, item["position_seconds"] / duration * 100), 1)
                if duration > 0
                else None
            )
        return items

    def clear_now_playing(self) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM now_playing")

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

    def record_watch_time(
        self,
        video_id: str,
        device: str,
        seconds: float,
        *,
        now: datetime | None = None,
    ) -> None:
        if not math.isfinite(seconds) or seconds <= 0:
            return
        timestamp = now or datetime.now(UTC)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        timestamp = timestamp.astimezone(UTC)
        day = timestamp.date().isoformat()
        with self._connection() as connection:
            if self.record_video_history:
                connection.execute(
                    """
                    UPDATE video_history
                    SET watched_seconds = watched_seconds + ?
                    WHERE id = (
                        SELECT id FROM video_history
                        WHERE video_id = ? AND device = ?
                        ORDER BY started_at DESC LIMIT 1
                    )
                    """,
                    (seconds, video_id, device),
                )
            connection.execute(
                """
                INSERT INTO watch_time_daily (day, watched_seconds) VALUES (?, ?)
                ON CONFLICT(day) DO UPDATE SET
                    watched_seconds = watched_seconds + excluded.watched_seconds
                """,
                (day, seconds),
            )
            bucket_start = datetime.fromtimestamp(
                int(timestamp.timestamp()) // 300 * 300, UTC
            ).isoformat()
            connection.execute(
                """
                INSERT INTO watch_time_buckets (bucket_start, watched_seconds) VALUES (?, ?)
                ON CONFLICT(bucket_start) DO UPDATE SET
                    watched_seconds = watched_seconds + excluded.watched_seconds
                """,
                (bucket_start, seconds),
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
                (
                    video_id if self.record_video_history else "",
                    device,
                    timestamp,
                    max(0.0, seconds_saved),
                    max(0, segment_count),
                ),
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

    def history(self, limit: int = 100, offset: int = 0, search: str = "") -> dict[str, Any]:
        search = search.strip()
        pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT video_id, title, channel, channel_url, thumbnail_url,
                    device, started_at, last_seen_at, segment_count,
                    watched_seconds, duration_seconds
                FROM video_history
                WHERE (? = '' OR video_id LIKE ? ESCAPE '\\'
                    OR title LIKE ? ESCAPE '\\' OR channel LIKE ? ESCAPE '\\'
                    OR device LIKE ? ESCAPE '\\')
                ORDER BY started_at DESC LIMIT ? OFFSET ?
                """,
                (search, pattern, pattern, pattern, pattern, limit, offset),
            ).fetchall()
            total = connection.execute(
                """
                SELECT COUNT(*) FROM video_history
                WHERE (? = '' OR video_id LIKE ? ESCAPE '\\'
                    OR title LIKE ? ESCAPE '\\' OR channel LIKE ? ESCAPE '\\'
                    OR device LIKE ? ESCAPE '\\')
                """,
                (search, pattern, pattern, pattern, pattern),
            ).fetchone()[0]
        items = []
        for row in rows:
            item = dict(row)
            duration = item["duration_seconds"]
            item["viewed_percent"] = (
                round(min(100.0, item["watched_seconds"] / duration * 100), 1)
                if duration > 0
                else None
            )
            items.append(item)
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "search": search,
        }

    def stats(
        self,
        timezone_name: str = "UTC",
        historical_period: str = "day",
    ) -> dict[str, Any]:
        if historical_period not in {"day", "week", "month", "year"}:
            raise ValueError("Historical period must be day, week, month, or year")
        timezone = ZoneInfo(timezone_name)
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
            watched_seconds = connection.execute(
                "SELECT COALESCE(SUM(watched_seconds), 0) FROM watch_time_daily"
            ).fetchone()[0]
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
            recent_watched_seconds = connection.execute(
                "SELECT COALESCE(SUM(watched_seconds), 0) FROM watch_time_daily WHERE day >= ?",
                (cutoff[:10],),
            ).fetchone()[0]
            top_videos = connection.execute(
                """
                SELECT video_id, title, channel, device, watched_seconds, segment_count
                FROM video_history
                WHERE watched_seconds > 0
                ORDER BY watched_seconds DESC, started_at DESC
                LIMIT 10
                """
            ).fetchall()
            daily = connection.execute(
                """
                WITH activity AS (
                    SELECT day, watched_seconds, 0 AS seconds_saved, 0 AS skip_events
                    FROM watch_time_daily WHERE day >= ?
                    UNION ALL
                    SELECT substr(skipped_at, 1, 10) AS day, 0 AS watched_seconds,
                        SUM(seconds_saved) AS seconds_saved, COUNT(*) AS skip_events
                    FROM skip_events WHERE skipped_at >= ? GROUP BY day
                )
                SELECT day, SUM(watched_seconds) AS watched_seconds,
                    SUM(seconds_saved) AS seconds_saved, SUM(skip_events) AS skip_events
                FROM activity
                GROUP BY day
                ORDER BY day
                """,
                (cutoff[:10], cutoff),
            ).fetchall()
            watch_buckets = connection.execute(
                "SELECT bucket_start, watched_seconds FROM watch_time_buckets"
            ).fetchall()
            skip_times = connection.execute(
                "SELECT skipped_at, seconds_saved FROM skip_events"
            ).fetchall()
        watched_by_hour = [0.0] * 24
        watched_by_weekday = [0.0] * 7
        skipped_by_hour = [0.0] * 24
        skipped_by_weekday = [0.0] * 7
        historical: dict[str, dict[str, Any]] = {}

        def period_key(local_time: datetime, granularity: str) -> str:
            if granularity == "day":
                return local_time.date().isoformat()
            if granularity == "week":
                iso_year, iso_week, _ = local_time.isocalendar()
                return f"{iso_year}-W{iso_week:02d}"
            if granularity == "month":
                return local_time.strftime("%Y-%m")
            return str(local_time.year)

        def historical_entry(
            local_time: datetime,
            watched: float = 0,
            skipped: float = 0,
            skip_events: int = 0,
        ) -> None:
            key = period_key(local_time, historical_period)
            entry = historical.setdefault(
                key,
                {
                    "period": key,
                    "watched_seconds": 0.0,
                    "seconds_saved": 0.0,
                    "skip_events": 0,
                },
            )
            entry["watched_seconds"] += watched
            entry["seconds_saved"] += skipped
            entry["skip_events"] += skip_events

        def complete_historical_periods() -> list[dict[str, Any]]:
            period_starts = {
                "day": lambda key: datetime.fromisoformat(key),
                "week": lambda key: datetime.strptime(f"{key}-1", "%G-W%V-%u"),
                "month": lambda key: datetime.strptime(f"{key}-01", "%Y-%m-%d"),
                "year": lambda key: datetime.strptime(f"{key}-01-01", "%Y-%m-%d"),
            }
            keys = sorted(historical)
            if not keys:
                return []
            current = period_starts[historical_period](keys[0])
            last = period_starts[historical_period](keys[-1])
            while current < last:
                key = period_key(current, historical_period)
                historical.setdefault(
                    key,
                    {
                        "period": key,
                        "watched_seconds": 0.0,
                        "seconds_saved": 0.0,
                        "skip_events": 0,
                    },
                )
                if historical_period == "day":
                    current += timedelta(days=1)
                elif historical_period == "week":
                    current += timedelta(weeks=1)
                elif historical_period == "month":
                    current = current.replace(
                        year=current.year + (current.month == 12),
                        month=current.month % 12 + 1,
                    )
                else:
                    current = current.replace(year=current.year + 1)
            return [
                {
                    **entry,
                    "watched_seconds": round(float(entry["watched_seconds"]), 1),
                    "seconds_saved": round(float(entry["seconds_saved"]), 1),
                }
                for _, entry in sorted(historical.items())
            ]

        for row in watch_buckets:
            local_time = datetime.fromisoformat(row["bucket_start"]).astimezone(timezone)
            watched_by_hour[local_time.hour] += row["watched_seconds"]
            watched_by_weekday[local_time.weekday()] += row["watched_seconds"]
            historical_entry(local_time, watched=row["watched_seconds"])
        for row in skip_times:
            local_time = datetime.fromisoformat(row["skipped_at"]).astimezone(timezone)
            skipped_by_hour[local_time.hour] += row["seconds_saved"]
            skipped_by_weekday[local_time.weekday()] += row["seconds_saved"]
            historical_entry(local_time, skipped=row["seconds_saved"], skip_events=1)
        return {
            "totals": {
                "videos_watched": video_count,
                "skip_events": totals["skip_events"],
                "segments_skipped": totals["segments_skipped"],
                "seconds_saved": round(totals["seconds_saved"], 1),
                "watched_seconds": round(watched_seconds, 1),
                "time_saved_days": round(totals["seconds_saved"] / 86400, 2),
            },
            "last_30_days": {
                "videos_watched": recent_videos,
                "skip_events": recent["skip_events"],
                "segments_skipped": recent["segments_skipped"],
                "seconds_saved": round(recent["seconds_saved"], 1),
                "watched_seconds": round(recent_watched_seconds, 1),
            },
            "by_category": [dict(row) for row in categories],
            "by_device": [dict(row) for row in devices],
            "daily": [dict(row) for row in daily],
            "historical": complete_historical_periods(),
            "top_videos": [dict(row) for row in top_videos],
            "by_hour": [
                {
                    "hour": hour,
                    "watched_seconds": round(watched_by_hour[hour], 1),
                    "seconds_saved": round(skipped_by_hour[hour], 1),
                }
                for hour in range(24)
            ],
            "by_weekday": [
                {
                    "weekday": weekday,
                    "watched_seconds": round(watched_by_weekday[weekday], 1),
                    "seconds_saved": round(skipped_by_weekday[weekday], 1),
                }
                for weekday in range(7)
            ],
        }

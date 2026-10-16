import asyncio
import copy
import logging
import math
import sys
import time
from urllib.parse import quote
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from iSponsorBlockTV import api_helpers, main as upstream_main
from iSponsorBlockTV.helpers import app_start

from .activity import ActivityStore


def _observed_watch_seconds(
    previous: tuple[str, bool, float, float] | None,
    video_id: str,
    position: float,
    observed_at: float,
) -> float:
    if not previous or previous[0] != video_id or not previous[1]:
        return 0.0
    elapsed = observed_at - previous[3]
    advanced = position - previous[2]
    if not math.isfinite(elapsed) or not math.isfinite(advanced):
        return 0.0
    return min(max(0.0, elapsed), max(0.0, advanced))


def _interval_seconds(intervals: list[tuple[float, float]]) -> float:
    total = 0.0
    end_of_previous = float("-inf")
    for start, end in sorted(intervals):
        if end <= start:
            continue
        total += max(0.0, end - max(start, end_of_previous))
        end_of_previous = max(end_of_previous, end)
    return total


def install_activity_hooks(store: ActivityStore) -> None:
    original_process_segments = api_helpers.ApiHelper.process_segments

    def process_segments(response: dict[str, Any], minimum_skip_length: float):
        raw_response = copy.deepcopy(response)
        segments, ignore_ttl = original_process_segments(response, minimum_skip_length)
        raw_by_uuid: dict[str, tuple[str, float, float]] = {}
        for raw_segment in raw_response.get("segments", []):
            bounds = raw_segment.get("segment", [])
            if len(bounds) != 2:
                continue
            raw_by_uuid[str(raw_segment.get("UUID", ""))] = (
                str(raw_segment.get("category", "unknown")),
                float(bounds[0]),
                float(bounds[1]),
            )

        for segment in segments:
            counts: Counter[str] = Counter()
            intervals: dict[str, list[tuple[float, float]]] = defaultdict(list)
            for uuid in segment.get("UUID", []):
                raw = raw_by_uuid.get(str(uuid))
                if not raw:
                    continue
                category, start, end = raw
                counts[category] += 1
                intervals[category].append((start, end))
            segment["category_counts"] = dict(counts)
            segment["category_seconds"] = {
                category: _interval_seconds(category_intervals)
                for category, category_intervals in intervals.items()
            }
        return segments, ignore_ttl

    api_helpers.ApiHelper.process_segments = staticmethod(process_segments)

    original_get_segments = api_helpers.ApiHelper.get_segments

    async def get_segments(self, video_id: str):
        segments = await original_get_segments(self, video_id)
        for listener in getattr(self, "_webui_listeners", []):
            if getattr(listener, "_webui_current_video_id", None) == video_id:
                store.update_video_segment_count(
                    video_id, listener.name, len(segments)
                )
        return segments

    api_helpers.ApiHelper.get_segments = get_segments

    original_process_playstatus = upstream_main.DeviceListener.process_playstatus

    async def update_video_metadata(listener: Any, video_id: str) -> None:
        thumbnail_url = f"https://i.ytimg.com/vi/{quote(video_id, safe='')}/hqdefault.jpg"
        try:
            async with listener.web_session.get(
                "https://www.youtube.com/oembed",
                params={
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "format": "json",
                },
                timeout=5,
            ) as response:
                if response.status != 200:
                    return
                metadata = await response.json()
            store.update_video_metadata(
                video_id=video_id,
                device=listener.name,
                title=str(metadata.get("title") or video_id),
                channel=str(metadata.get("author_name") or ""),
                channel_url=str(metadata.get("author_url") or ""),
                thumbnail_url=str(metadata.get("thumbnail_url") or thumbnail_url),
            )
        except Exception:
            listener.logger.debug("Could not retrieve YouTube metadata for %s", video_id, exc_info=True)

    async def process_playstatus(self, state: Any, time_start: float):
        video_id = getattr(state, "videoId", "") or ""
        is_playing = getattr(getattr(state, "state", None), "value", None) == 1
        try:
            position = float(getattr(state, "currentTime", 0) or 0)
        except (TypeError, ValueError):
            position = 0.0
        previous = getattr(self, "_webui_last_playback", None)
        watched_seconds = _observed_watch_seconds(previous, video_id, position, time_start)
        if watched_seconds:
            store.record_watch_time(video_id, self.name, watched_seconds)
        self._webui_last_playback = (video_id, is_playing, position, time_start)
        store.set_now_playing(video_id, self.name, playing=is_playing)
        if video_id and is_playing:
            self._webui_current_video_id = video_id
            self.api_helper._webui_listeners = getattr(
                self.api_helper, "_webui_listeners", set()
            )
            self.api_helper._webui_listeners.add(self)
            store.record_video(video_id, video_id, self.name, 0)
            if getattr(self, "_webui_metadata_video_id", None) != video_id:
                self._webui_metadata_video_id = video_id
                self._webui_metadata_task = asyncio.create_task(
                    update_video_metadata(self, video_id)
                )
        return await original_process_playstatus(self, state, time_start)

    upstream_main.DeviceListener.process_playstatus = process_playstatus

    original_skip = upstream_main.DeviceListener.skip

    async def skip(self, time_to: float, position: float, uuids: list[str]):
        await original_skip(self, time_to, position, uuids)
        current_video_id = getattr(self, "_webui_current_video_id", "")
        if current_video_id:
            store.record_watch_time(current_video_id, self.name, max(0.0, time_to))
        current_playback = getattr(self, "_webui_last_playback", None)
        if current_playback and current_playback[1]:
            self._webui_last_playback = (
                current_playback[0],
                True,
                float(position),
                time.monotonic(),
            )
        candidates = getattr(self, "_webui_segments", [])
        skipped = next(
            (segment for segment in candidates if set(uuids).intersection(segment.get("UUID", []))),
            None,
        )
        if skipped and current_video_id:
            store.record_skip(
                video_id=current_video_id,
                device=self.name,
                seconds_saved=float(skipped["end"]) - float(skipped["start"]),
                segment_count=len(uuids),
                category_counts=skipped.get("category_counts", {}),
                category_seconds=skipped.get("category_seconds", {}),
            )

    upstream_main.DeviceListener.skip = skip

    original_time_to_segment = upstream_main.DeviceListener.time_to_segment

    async def time_to_segment(self, segments: list[dict[str, Any]], position: float, time_start: float):
        self._webui_segments = segments
        return await original_time_to_segment(self, segments, position, time_start)

    upstream_main.DeviceListener.time_to_segment = time_to_segment


def main() -> None:
    if "--help" in sys.argv or "-h" in sys.argv:
        app_start()
        return
    data_dir = Path("./data")
    for index, argument in enumerate(sys.argv[:-1]):
        if argument == "--data":
            data_dir = Path(sys.argv[index + 1])
            break
    install_activity_hooks(ActivityStore(data_dir / "activity.sqlite3"))
    app_start()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()

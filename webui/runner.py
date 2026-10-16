import asyncio
import copy
import logging
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from iSponsorBlockTV import api_helpers, main as upstream_main
from iSponsorBlockTV.helpers import app_start

from .activity import ActivityStore


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
                store.record_video(
                    video_id,
                    listener._webui_title,
                    listener.name,
                    len(segments),
                )
        return segments

    api_helpers.ApiHelper.get_segments = get_segments

    original_process_playstatus = upstream_main.DeviceListener.process_playstatus

    async def process_playstatus(self, state: Any, time_start: float):
        video_id = getattr(state, "videoId", "") or ""
        is_playing = getattr(getattr(state, "state", None), "value", None) == 1
        if video_id and is_playing:
            title = next(
                (
                    str(value)
                    for name in ("videoTitle", "title", "video_title")
                    if (value := getattr(state, name, None))
                ),
                video_id,
            )
            self._webui_current_video_id = video_id
            self._webui_title = title
            self.api_helper._webui_listeners = getattr(
                self.api_helper, "_webui_listeners", set()
            )
            self.api_helper._webui_listeners.add(self)
            store.record_video(video_id, title, self.name, 0)
        return await original_process_playstatus(self, state, time_start)

    upstream_main.DeviceListener.process_playstatus = process_playstatus

    original_skip = upstream_main.DeviceListener.skip

    async def skip(self, time_to: float, position: float, uuids: list[str]):
        await original_skip(self, time_to, position, uuids)
        current_video_id = getattr(self, "_webui_current_video_id", "")
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

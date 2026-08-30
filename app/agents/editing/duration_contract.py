from __future__ import annotations

from typing import Any


def template_slot_durations_ms(video_editing_db: dict[str, Any]) -> dict[int, int]:
    """Return positive per-scene output limits from the selected template."""

    shooting_guide = video_editing_db.get("shooting_guide") or {}
    scenes = shooting_guide.get("scenes") or []
    durations: dict[int, int] = {}
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        try:
            order = int(scene.get("scene_order"))
            duration_ms = int(round(float(scene.get("target_duration_sec")) * 1000))
        except (TypeError, ValueError):
            continue
        if order > 0 and duration_ms > 0:
            durations[order] = duration_ms
    return durations


def fit_frame_exact_window(
    trim_in_ms: int,
    trim_out_ms: int,
    timestamps: list[int],
    *,
    min_duration_ms: int,
    max_duration_ms: int,
) -> tuple[int, int]:
    """Fit a selected cut to a slot without inventing non-frame boundaries.

    The longest valid sub-window wins. Boundary displacement is the tie-breaker,
    preserving as much of the model-selected semantic interval as possible.
    When sampled evidence is too sparse to provide a valid sub-window, the
    original range is retained so the recipe-level speed guard can fit it.
    """

    if trim_out_ms - trim_in_ms <= max_duration_ms:
        return trim_in_ms, trim_out_ms

    selected = sorted(
        {
            int(timestamp)
            for timestamp in timestamps
            if trim_in_ms <= int(timestamp) <= trim_out_ms
        }
    )
    best: tuple[int, int] | None = None
    best_score: tuple[int, int, int] | None = None
    for index, start in enumerate(selected):
        for end in selected[index + 1 :]:
            duration = end - start
            if duration < min_duration_ms:
                continue
            if duration > max_duration_ms:
                break
            score = (
                -duration,
                abs(start - trim_in_ms) + abs(end - trim_out_ms),
                start,
            )
            if best_score is None or score < best_score:
                best = (start, end)
                best_score = score
    return best or (trim_in_ms, trim_out_ms)

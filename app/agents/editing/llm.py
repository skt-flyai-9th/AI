from __future__ import annotations

import base64
import hashlib
import io
import json
import math
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel
import numpy as np
from PIL import Image

from app.agents.editing.context_builder import build_editing_context
from app.agents.editing.effect_planner import EffectPlanner
from app.agents.editing.structured_output import (
    EditingLLMError,
    request_structured_model,
)
from app.agents.editing.types import (
    EditingPlanDecision,
    FrameBatchAnalysis,
    FrameObservation,
    SourceCutPlan,
    VideoContext,
)
from app.agents.editing.reals import get_reals_registry
from app.core.config import Settings, get_settings


class EditingLLM(Protocol):
    def plan_recipe(
        self,
        *,
        domain_context: str,
        project: dict[str, Any],
        selected_shortform: dict[str, Any],
        video_editing_db: dict[str, Any],
        video_contexts: list[VideoContext],
        parent_recipe: dict[str, Any] | None,
        revision_action: str | None,
        progress_callback: Callable[[int], None] | None = None,
        checkpoint_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> EditingPlanDecision: ...

    def repair_recipe(
        self,
        *,
        domain_context: str,
        project: dict[str, Any],
        selected_shortform: dict[str, Any],
        video_editing_db: dict[str, Any],
        video_contexts: list[VideoContext],
        decision: EditingPlanDecision,
        validation_errors: list[dict[str, Any]],
        parent_recipe: dict[str, Any] | None,
        revision_action: str | None,
        progress_callback: Callable[[int], None] | None = None,
        checkpoint_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> EditingPlanDecision: ...


class OpenAIEditingLLM:
    """Frame-accurate editing planner on top of the Responses API.

    Source videos are uniformly sampled under per-video and per-run budgets
    before source trimming. The sampled exact timestamps remain the only legal
    cut boundaries and the resulting observations are reused on the timeline.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self.api_key = settings.openai_api_key.strip()
        self.base_url = settings.openai_base_url.rstrip("/")
        self.model = settings.editing_openai_model.strip()
        self.timeout = settings.editing_request_timeout_seconds
        self.max_output_tokens = settings.editing_max_output_tokens
        self.max_request_attempts = settings.editing_llm_max_request_attempts
        self.rate_limit_retry_base_seconds = settings.editing_rate_limit_retry_base_seconds
        self.analysis_batch_frames = int(getattr(settings, "editing_analysis_batch_frames", 24))
        self.analysis_max_frames_per_video = int(
            getattr(settings, "editing_analysis_max_frames_per_video", 48)
        )
        self.analysis_max_total_frames = int(
            getattr(settings, "editing_analysis_max_total_frames", 120)
        )
        self.effect_planner = EffectPlanner(settings=settings)
        self._analysis_cache: dict[str, dict[str, Any]] = {}

    def plan_recipe(
        self,
        *,
        domain_context: str,
        project: dict[str, Any],
        selected_shortform: dict[str, Any],
        video_editing_db: dict[str, Any],
        video_contexts: list[VideoContext],
        parent_recipe: dict[str, Any] | None,
        revision_action: str | None,
        progress_callback: Callable[[int], None] | None = None,
        checkpoint_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> EditingPlanDecision:
        reduced_structure = _is_reduced_structure_revision(revision_action)
        if reduced_structure:
            task = (
                "Create a complete reduced-structure EditRecipe from the available supplied "
                "videos. The user explicitly accepted omitting unsupported scene roles."
            )
        else:
            task = "Revise the parent EditRecipe" if revision_action else "Create an EditRecipe"
        reference_context = video_editing_db.get("reference_evidence") or {}
        shoot_mode = _resolve_shoot_mode(project, video_contexts)
        if _is_information_format(video_editing_db):
            shoot_mode = "MULTI_CUT"
        cache_key = _analysis_cache_key(selected_shortform, video_contexts, shoot_mode)
        prepared = self._analysis_cache.get(cache_key)
        if prepared is None:
            prepared = self._prepare_frame_analysis(
                video_contexts=video_contexts,
                video_editing_db=video_editing_db,
                reference_context=reference_context,
                revision_action=revision_action,
                shoot_mode=shoot_mode,
                progress_callback=progress_callback,
            )
            self._analysis_cache[cache_key] = prepared
            if checkpoint_callback is not None:
                checkpoint_callback({"cache_key": cache_key, "prepared": prepared})
        editing_context = build_editing_context(
            project=project,
            selected_shortform=selected_shortform,
            video_editing_db=video_editing_db,
            video_contexts=video_contexts,
            prepared_analysis=prepared,
        )
        payload = {
            "task": task,
            "project": project,
            "selected_shortform": selected_shortform,
            "video_editing_db": video_editing_db,
            "reference_original_context": reference_context,
            "source_preparation": prepared["source_preparation"],
            "one_take_overview": prepared.get("one_take_overview"),
            "produced_frame_context": prepared["produced_frame_context"],
            "editing_context": editing_context,
            "parent_recipe": parent_recipe,
            "revision_action": revision_action,
            "source_gap_policy": (
                {
                    "mode": "USE_REDUCED_STRUCTURE",
                    "must_return_recipe": True,
                    "instruction": (
                        "Use a coherent subset of the supplied footage in shooting order. "
                        "Do not return SOURCE_GAP again for roles the user chose to omit."
                    ),
                }
                if reduced_structure
                else {"mode": "DETECT_REQUIRED_ROLE_GAPS"}
            ),
            "renderer_capabilities": _renderer_capabilities(),
            "requirements": _requirements(
                prepared["source_preparation"],
                reduced_structure=reduced_structure,
            ),
        }
        decision = self._request_model(
            schema_model=EditingPlanDecision,
            instructions=domain_context,
            user_payload=payload,
            schema_name="editing_plan",
        )
        prepared_decision = _apply_source_preparation(
            decision,
            prepared["source_preparation"],
            video_contexts,
        )
        return self.effect_planner.apply(
            prepared_decision,
            produced_frame_context=prepared["produced_frame_context"],
            video_editing_db=video_editing_db,
        )

    def repair_recipe(
        self,
        *,
        domain_context: str,
        project: dict[str, Any],
        selected_shortform: dict[str, Any],
        video_editing_db: dict[str, Any],
        video_contexts: list[VideoContext],
        decision: EditingPlanDecision,
        validation_errors: list[dict[str, Any]],
        parent_recipe: dict[str, Any] | None,
        revision_action: str | None,
        progress_callback: Callable[[int], None] | None = None,
        checkpoint_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> EditingPlanDecision:
        shoot_mode = _resolve_shoot_mode(project, video_contexts)
        if _is_information_format(video_editing_db):
            shoot_mode = "MULTI_CUT"
        cache_key = _analysis_cache_key(selected_shortform, video_contexts, shoot_mode)
        prepared = self._analysis_cache.get(cache_key)
        if prepared is None:
            prepared = self._prepare_frame_analysis(
                video_contexts=video_contexts,
                video_editing_db=video_editing_db,
                reference_context=video_editing_db.get("reference_evidence") or {},
                revision_action=revision_action,
                shoot_mode=shoot_mode,
                progress_callback=progress_callback,
            )
            self._analysis_cache[cache_key] = prepared
            if checkpoint_callback is not None:
                checkpoint_callback({"cache_key": cache_key, "prepared": prepared})
        editing_context = build_editing_context(
            project=project,
            selected_shortform=selected_shortform,
            video_editing_db=video_editing_db,
            video_contexts=video_contexts,
            prepared_analysis=prepared,
        )
        payload = {
            "task": "Repair the EditRecipe so every deterministic validation error is fixed.",
            "project": project,
            "selected_shortform": selected_shortform,
            "video_editing_db": video_editing_db,
            "reference_original_context": prepared["reference_context"],
            "source_preparation": prepared["source_preparation"],
            "one_take_overview": prepared.get("one_take_overview"),
            "produced_frame_context": prepared["produced_frame_context"],
            "editing_context": editing_context,
            "invalid_decision": decision.model_dump(mode="json"),
            "validation_errors": validation_errors,
            "parent_recipe": parent_recipe,
            "revision_action": revision_action,
            "renderer_capabilities": _renderer_capabilities(),
            "requirements": _requirements(prepared["source_preparation"]),
        }
        repaired = self._request_model(
            schema_model=EditingPlanDecision,
            instructions=domain_context,
            user_payload=payload,
            schema_name="editing_plan_repair",
        )
        prepared_decision = _apply_source_preparation(
            repaired,
            prepared["source_preparation"],
            video_contexts,
        )
        return self.effect_planner.apply(
            prepared_decision,
            produced_frame_context=prepared["produced_frame_context"],
            video_editing_db=video_editing_db,
        )

    def restore_analysis_checkpoint(self, checkpoint: dict[str, Any] | None) -> None:
        if not checkpoint:
            return
        cache_key = str(checkpoint.get("cache_key") or "")
        prepared = checkpoint.get("prepared")
        if cache_key and isinstance(prepared, dict):
            self._analysis_cache[cache_key] = prepared

    def _prepare_frame_analysis(
        self,
        *,
        video_contexts: list[VideoContext],
        video_editing_db: dict[str, Any],
        reference_context: dict[str, Any],
        revision_action: str | None,
        shoot_mode: str,
        progress_callback: Callable[[int], None] | None = None,
    ) -> dict[str, Any]:
        sampled_by_video = _sample_video_frames(
            video_contexts,
            max_per_video=max(1, getattr(self, "analysis_max_frames_per_video", 48)),
            max_total=max(1, getattr(self, "analysis_max_total_frames", 120)),
        )
        batch_size = max(1, min(getattr(self, "analysis_batch_frames", 24), 40))
        total_batches = sum(
            math.ceil(len(sampled_by_video[context.video_id]) / batch_size)
            for context in video_contexts
        )
        completed_batches = 0

        def report_batch_complete() -> None:
            nonlocal completed_batches
            completed_batches += 1
            if progress_callback is not None and total_batches:
                progress_callback(35 + int(23 * completed_batches / total_batches))

        if shoot_mode == "MULTI_CUT":
            analyzed = [
                self._analyze_video_frames(
                    context=context,
                    frames=sampled_by_video[context.video_id],
                    purpose="MULTI_CUT_SAMPLED_EXACT_FRAMES",
                    reference_context=reference_context,
                    video_editing_db=video_editing_db,
                    on_batch_complete=report_batch_complete,
                )
                for context in video_contexts
            ]
            source_plan = self._plan_source_cuts(
                video_contexts=video_contexts,
                analyzed=analyzed,
                video_editing_db=video_editing_db,
                reference_context=reference_context,
                revision_action=revision_action,
            )
            source_plan = _normalize_source_cut_plan(
                source_plan,
                video_contexts,
                analyzed,
                min_cut_ms=_source_min_cut_ms(video_editing_db),
            )
            produced = _map_cut_analysis_to_produced(analyzed, source_plan)
            return {
                "reference_context": reference_context,
                "source_preparation": source_plan.model_dump(mode="json"),
                "produced_frame_context": produced,
            }

        if len(video_contexts) != 1:
            raise EditingLLMError("ONE_TAKE requires exactly one source video.", retryable=False)
        context = video_contexts[0]
        detailed = self._analyze_video_frames(
            context=context,
            frames=sampled_by_video[context.video_id],
            purpose="ONE_TAKE_SAMPLED_EXACT_FRAMES",
            reference_context=reference_context,
            video_editing_db=video_editing_db,
            on_batch_complete=report_batch_complete,
        )
        produced_observations = []
        for observation in detailed["observations"]:
            item = dict(observation)
            item["produced_timestamp_ms"] = item["timestamp_ms"]
            produced_observations.append(item)
        return {
            "reference_context": reference_context,
            "source_preparation": {
                "mode": "ONE_TAKE_PASSTHROUGH",
                "video_id": context.video_id,
                "trim_in_ms": 0,
                "trim_out_ms": context.duration_ms,
            },
            "one_take_overview": detailed,
            "produced_frame_context": {
                "mode": "ONE_TAKE",
                "duration_ms": context.duration_ms,
                "summary": detailed["summary"],
                "observations": produced_observations,
            },
        }

    def _analyze_video_frames(
        self,
        *,
        context: VideoContext,
        frames: list[Any],
        purpose: str,
        reference_context: dict[str, Any],
        video_editing_db: dict[str, Any],
        prior_summary: str | None = None,
        on_batch_complete: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        if not frames:
            raise EditingLLMError(
                f"No frame evidence for video_id={context.video_id}.", retryable=False
            )
        observations: list[dict[str, Any]] = []
        summaries: list[str] = []
        batch_size = max(1, min(self.analysis_batch_frames, 40))

        def request_batch(batch: list[Any]) -> FrameBatchAnalysis:
            payload = {
                "task": (
                    "Analyze every supplied frame independently and in temporal context. "
                    "Return one observation for every input frame. Identify action phase, subject "
                    "position/scale, composition, camera/motion, rotation, quality, semantic event, "
                    "and whether this exact frame is a natural cut-transition boundary."
                ),
                "purpose": purpose,
                "video": {
                    "video_id": context.video_id,
                    "duration_ms": context.duration_ms,
                    "width": context.width,
                    "height": context.height,
                    "fps": context.fps,
                    "shooting_scene_order": context.shooting_scene_order,
                },
                "reference_original_context": reference_context,
                "video_editing_db": {
                    "shooting_guide": video_editing_db.get("shooting_guide") or {},
                    "editing_rules": video_editing_db.get("editing_rules") or {},
                },
                "prior_summary": prior_summary,
                "frame_manifest": [
                    {"frame_index": item.frame_index, "timestamp_ms": item.timestamp_ms}
                    for item in batch
                ],
                "coordinate_rules": {
                    "subject_x_y": "normalized 0..1 from top-left",
                    "subject_scale": "fraction of frame occupied by primary subject, 0..1",
                    "rotation_deg": "observed visual/camera tilt, clockwise positive",
                    "cut_transition_score": "0..1 confidence this exact frame is a natural boundary",
                },
            }
            content: list[dict[str, Any]] = [
                {
                    "type": "input_text",
                    "text": json.dumps(payload, ensure_ascii=False, default=str),
                }
            ]
            for frame in batch:
                content.append(
                    {
                        "type": "input_text",
                        "text": (
                            f"video_id={context.video_id}, frame_index={frame.frame_index}, "
                            f"timestamp_ms={frame.timestamp_ms}"
                        ),
                    }
                )
                content.append(
                    {"type": "input_image", "image_url": frame.image_url, "detail": "low"}
                )
            return self._request_model(
                schema_model=FrameBatchAnalysis,
                instructions=(
                    "You are the frame-accurate vision stage of SARILS Editing Agent. "
                    "Use the reference-original context as the target editing grammar, but never "
                    "invent content absent from the user frame. A cut boundary must be grounded in "
                    "the observed action/state transition. Use the same semantic-event, composition, "
                    "camera and transform concepts present in Gemini reference evidence so later "
                    "effects can reproduce the original-video grammar."
                ),
                user_payload=None,
                schema_name="editing_frame_batch",
                content_override=content,
                timeout_max_attempts=1,
            )

        for start in range(0, len(frames), batch_size):
            batch = frames[start : start + batch_size]
            try:
                batch_results = [(batch, request_batch(batch))]
            except EditingLLMError as exc:
                if exc.reason != "timeout" or len(batch) < 2:
                    raise
                midpoint = (len(batch) + 1) // 2
                split_batches = (batch[:midpoint], batch[midpoint:])
                batch_results = [(part, request_batch(part)) for part in split_batches if part]
            if on_batch_complete is not None:
                on_batch_complete()
            for analyzed_batch, result in batch_results:
                summaries.append(result.summary)
                by_index = {item.frame_index: item for item in result.observations}
                for frame in analyzed_batch:
                    observed = by_index.get(frame.frame_index)
                    if observed is None:
                        observed = FrameObservation(
                            video_id=context.video_id,
                            frame_index=frame.frame_index,
                            timestamp_ms=frame.timestamp_ms,
                        )
                    else:
                        observed = observed.model_copy(
                            update={
                                "video_id": context.video_id,
                                "frame_index": frame.frame_index,
                                "timestamp_ms": frame.timestamp_ms,
                            }
                        )
                    observations.append(observed.model_dump(mode="json"))
        return {
            "video_id": context.video_id,
            "shooting_scene_order": context.shooting_scene_order,
            "duration_ms": context.duration_ms,
            "fps": context.fps,
            "summary": " ".join(value for value in summaries if value)[:8000],
            "observations": observations,
        }

    def _plan_source_cuts(
        self,
        *,
        video_contexts: list[VideoContext],
        analyzed: list[dict[str, Any]],
        video_editing_db: dict[str, Any],
        reference_context: dict[str, Any],
        revision_action: str | None,
    ) -> SourceCutPlan:
        min_cut_ms = _source_min_cut_ms(video_editing_db)
        is_information = _is_information_format(video_editing_db)
        payload = {
            "task": (
                "Create the MULTI_CUT source-preparation plan before creative editing. "
                "Map user footage intervals to the most corresponding reference-original segments. "
                "Choose trim boundaries only on supplied exact-frame timestamps, preferring frames "
                "marked as natural cut-transition candidates. The reference "
                "segment context is the target structure; actual user footage is the hard evidence constraint."
            ),
            "reference_original_context": reference_context,
            "video_editing_db": video_editing_db,
            "revision_action": revision_action,
            "raw_frame_analysis": analyzed,
            "source_strategy": ("INFORMATIONAL_REASSEMBLY" if is_information else "CUT_PER_INPUT"),
            "hard_rules": [
                (
                    "For information-form footage, create one ordered decision per required reference edit segment. "
                    "The same video_id may appear multiple times, but its selected time ranges must never overlap."
                    if is_information
                    else "Preserve raw capture order and create exactly one source cut per input video."
                ),
                "Do not invent a reference segment that is not present in reference context/guide.",
                "trim_in_ms and trim_out_ms must exactly equal observed frame timestamps.",
                f"Every selected source cut must span at least {min_cut_ms}ms before creative speed changes.",
                "Prefer action/state boundaries and transition-candidate frames over arbitrary times.",
                "Keep the selected user content as similar as possible to the corresponding reference segment.",
            ],
        }
        result = self._request_model(
            schema_model=SourceCutPlan,
            instructions=(
                "You plan only source cuts. Do not choose captions, effects, color, zoom, or publishing copy."
            ),
            user_payload=payload,
            schema_name="editing_source_cut_plan",
        )
        return result.model_copy(
            update={"strategy": ("INFORMATIONAL_REASSEMBLY" if is_information else "CUT_PER_INPUT")}
        )

    def _request_model(
        self,
        *,
        schema_model: type[_ModelT],
        instructions: str,
        user_payload: dict[str, Any] | None,
        schema_name: str,
        content_override: list[dict[str, Any]] | None = None,
        timeout_max_attempts: int | None = None,
    ) -> _ModelT:
        if not self.api_key or not self.model:
            raise EditingLLMError(
                "OPENAI_API_KEY or EDITING_OPENAI_MODEL is not configured.",
                retryable=False,
            )
        content = content_override
        if content is None:
            content = [
                {
                    "type": "input_text",
                    "text": json.dumps(user_payload or {}, ensure_ascii=False, default=str),
                }
            ]
        schema = _make_strict_schema(schema_model.model_json_schema())
        return request_structured_model(
            schema_model=schema_model,
            schema=schema,
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model,
            instructions=instructions,
            content=content,
            schema_name=schema_name,
            timeout=self.timeout,
            max_output_tokens=self.max_output_tokens,
            max_attempts=self.max_request_attempts,
            rate_limit_retry_base_seconds=self.rate_limit_retry_base_seconds,
            timeout_max_attempts=timeout_max_attempts,
        )


def _sample_video_frames(
    contexts: list[VideoContext],
    *,
    max_per_video: int,
    max_total: int,
) -> dict[str, list[Any]]:
    """Scan every extracted frame on CPU, then retain changes plus temporal coverage."""
    desired = [min(len(context.keyframes), max_per_video) for context in contexts]
    budgets = desired.copy()
    while sum(budgets) > max_total:
        candidate = max(
            (index for index, value in enumerate(budgets) if value > 1),
            key=lambda index: (budgets[index], desired[index], -index),
            default=None,
        )
        if candidate is None:
            break
        budgets[candidate] -= 1
    return {
        context.video_id: _adaptive_sample(context.keyframes, budgets[index])
        for index, context in enumerate(contexts)
    }


def _adaptive_sample(frames: list[Any], limit: int) -> list[Any]:
    if limit <= 0 or not frames:
        return []
    if len(frames) <= limit:
        return list(frames)
    signatures = [_frame_signature(frame.image_url) for frame in frames]
    if any(signature is None for signature in signatures):
        return _uniform_sample(frames, limit)

    changes = [
        (
            float(np.mean(np.abs(signatures[index] - signatures[index - 1]))),
            index,
        )
        for index in range(1, len(frames))
    ]
    selected = {0, len(frames) - 1}
    for score, index in sorted(changes, reverse=True):
        if score <= 0:
            break
        for candidate in (index - 1, index):
            if len(selected) >= limit:
                break
            selected.add(candidate)
        if len(selected) >= max(2, limit // 2):
            break

    for frame in _uniform_sample(frames, limit):
        if len(selected) >= limit:
            break
        selected.add(frames.index(frame))
    if len(selected) < limit:
        selected.update(index for index in range(len(frames)) if len(selected) < limit)
    return [frames[index] for index in sorted(selected)[:limit]]


def _frame_signature(image_url: str) -> np.ndarray | None:
    try:
        encoded = image_url.split(",", 1)[1]
        raw = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(raw)) as image:
            return np.asarray(image.convert("L").resize((16, 16)), dtype=np.float32) / 255.0
    except (ValueError, OSError, IndexError):
        return None


def _uniform_sample(frames: list[Any], limit: int) -> list[Any]:
    if limit <= 0 or not frames:
        return []
    if len(frames) <= limit:
        return list(frames)
    if limit == 1:
        return [frames[0]]
    indices = {round(position * (len(frames) - 1) / (limit - 1)) for position in range(limit)}
    return [frames[index] for index in sorted(indices)]


def _resolve_shoot_mode(project: dict[str, Any], contexts: list[VideoContext]) -> str:
    explicit = str(project.get("shoot_mode") or "").strip().upper()
    if explicit in {"MULTI_CUT", "CUT"}:
        return "MULTI_CUT"
    if explicit in {"ONE_TAKE", "ONETAKE"}:
        return "ONE_TAKE"
    return "ONE_TAKE" if len(contexts) == 1 else "MULTI_CUT"


def _nearest_timestamp(value: int, timestamps: list[int]) -> int:
    if not timestamps:
        return value
    return min(timestamps, key=lambda item: abs(item - value))


def _source_min_cut_ms(video_editing_db: dict[str, Any]) -> int:
    rules = video_editing_db.get("editing_rules") or {}
    registry_min = int(get_reals_registry().edit_policies.get("min_cut_duration_ms", 300))
    try:
        database_min = int(rules.get("min_cut_duration_ms") or 0)
    except (TypeError, ValueError):
        database_min = 0
    return max(registry_min, database_min, 1)


def _expand_frame_exact_window(
    trim_in: int,
    trim_out: int,
    timestamps: list[int],
    min_cut_ms: int,
) -> tuple[int, int]:
    if trim_out - trim_in >= min_cut_ms:
        return trim_in, trim_out

    best: tuple[int, int] | None = None
    best_cost: tuple[int, int] | None = None
    for start in timestamps:
        if start > trim_in:
            break
        minimum_end = max(trim_out, start + min_cut_ms)
        end = next((item for item in timestamps if item >= minimum_end), None)
        if end is None:
            continue
        cost = (end - start, abs(start - trim_in) + abs(end - trim_out))
        if best_cost is None or cost < best_cost:
            best = (start, end)
            best_cost = cost
    if best is not None:
        return best

    if timestamps[-1] - timestamps[0] >= min_cut_ms:
        return timestamps[0], timestamps[-1]
    raise EditingLLMError(
        "Frame-exact source evidence cannot satisfy the minimum cut duration.",
        retryable=False,
    )


def _normalize_source_cut_plan(
    plan: SourceCutPlan,
    contexts: list[VideoContext],
    analyzed: list[dict[str, Any]],
    *,
    min_cut_ms: int = 300,
) -> SourceCutPlan:
    """Snap GPT cuts to real frames, preserve order, and prevent repair deadlocks."""
    analyzed_by_video = {item["video_id"]: item for item in analyzed}
    contexts_by_video = {item.video_id: item for item in contexts}
    normalized = []
    ordered_cuts = list(plan.cuts)
    if plan.strategy == "CUT_PER_INPUT":
        by_video = {item.video_id: item for item in plan.cuts}
        missing = [item.video_id for item in contexts if item.video_id not in by_video]
        if missing:
            raise EditingLLMError(
                f"Source-cut plan omitted video_id={missing[0]}.",
                retryable=False,
            )
        ordered_cuts = [
            by_video[item.video_id]
            for item in sorted(contexts, key=lambda value: value.shooting_scene_order)
        ]
    for cut in ordered_cuts:
        context = contexts_by_video.get(cut.video_id)
        if context is None:
            raise EditingLLMError(
                f"Source-cut plan referenced unknown video_id={cut.video_id}.",
                retryable=False,
            )
        source = analyzed_by_video.get(context.video_id) or {}
        timestamps = sorted(
            {
                int(item["timestamp_ms"])
                for item in source.get("observations", [])
                if int(item["timestamp_ms"]) >= 0
            }
        )
        if len(timestamps) < 2:
            raise EditingLLMError(
                f"Frame-exact source evidence is incomplete for video_id={context.video_id}.",
                retryable=False,
            )
        trim_in = _nearest_timestamp(cut.trim_in_ms, timestamps)
        trim_out = _nearest_timestamp(cut.trim_out_ms, timestamps)
        if trim_out <= trim_in:
            trim_in, trim_out = timestamps[0], timestamps[-1]
        try:
            trim_in, trim_out = _expand_frame_exact_window(
                trim_in,
                trim_out,
                timestamps,
                min_cut_ms,
            )
        except EditingLLMError as exc:
            raise EditingLLMError(
                f"{exc} video_id={context.video_id}, min_cut_ms={min_cut_ms}.",
                retryable=False,
            ) from exc
        normalized.append(
            cut.model_copy(
                update={
                    "trim_in_ms": trim_in,
                    "trim_out_ms": trim_out,
                }
            )
        )
    return SourceCutPlan(
        strategy=plan.strategy,
        cuts=normalized,
        rationale=plan.rationale,
    )


def _apply_source_preparation(
    decision: EditingPlanDecision,
    source_preparation: dict[str, Any],
    contexts: list[VideoContext],
) -> EditingPlanDecision:
    """Make source-preparation boundaries deterministic before validation/render."""
    if decision.outcome != "RECIPE" or decision.recipe is None:
        return decision

    recipe = decision.recipe.model_copy(deep=True)
    mode = source_preparation.get("mode")
    if mode == "MULTI_CUT":
        cuts = list(source_preparation.get("cuts") or [])
        clips = sorted(recipe.timeline, key=lambda item: item.clip_order)
        if len(clips) != len(cuts):
            raise EditingLLMError(
                "MULTI_CUT final recipe must contain one clip per prepared source interval.",
                retryable=False,
            )
        cursor = 0.0
        normalized_timeline = []
        for index, (cut, clip) in enumerate(zip(cuts, clips, strict=True), start=1):
            video_id = str(cut["video_id"])
            trim_in = int(cut["trim_in_ms"])
            trim_out = int(cut["trim_out_ms"])
            clip = clip.model_copy(
                update={
                    "clip_order": index,
                    "video_id": video_id,
                    "source_start_ms": trim_in,
                    "source_end_ms": trim_out,
                    "timeline_start_ms": int(round(cursor)),
                }
            )
            normalized_timeline.append(clip)
            cursor += (trim_out - trim_in) / clip.speed
        recipe.timeline = normalized_timeline
    else:
        if len(recipe.timeline) != 1 or len(contexts) != 1:
            raise EditingLLMError(
                "ONE_TAKE final recipe must contain exactly one source clip.",
                retryable=False,
            )
        context = contexts[0]
        clip = recipe.timeline[0]
        if clip.video_id != context.video_id:
            raise EditingLLMError(
                "ONE_TAKE final recipe references the wrong video.",
                retryable=False,
            )
        recipe.timeline = [
            clip.model_copy(
                update={
                    "clip_order": 1,
                    "source_start_ms": 0,
                    "source_end_ms": context.duration_ms,
                    "timeline_start_ms": 0,
                }
            )
        ]
    return decision.model_copy(update={"recipe": recipe})


def _map_cut_analysis_to_produced(
    analyzed: list[dict[str, Any]],
    source_plan: SourceCutPlan,
) -> dict[str, Any]:
    by_video = {item["video_id"]: item for item in analyzed}
    cursor = 0
    output: list[dict[str, Any]] = []
    for cut in source_plan.cuts:
        source = by_video.get(cut.video_id)
        if source is None:
            continue
        for observation in source["observations"]:
            timestamp = int(observation["timestamp_ms"])
            if timestamp < cut.trim_in_ms or timestamp > cut.trim_out_ms:
                continue
            item = dict(observation)
            item["mapped_reference_segment_id"] = cut.mapped_reference_segment_id
            item["produced_timestamp_ms"] = cursor + timestamp - cut.trim_in_ms
            output.append(item)
        cursor += cut.trim_out_ms - cut.trim_in_ms
    return {
        "mode": "MULTI_CUT",
        "duration_ms": cursor,
        "summary": source_plan.rationale,
        "observations": output,
    }


def _analysis_cache_key(
    selected_shortform: dict[str, Any],
    contexts: list[VideoContext],
    shoot_mode: str,
) -> str:
    payload = {
        "template": selected_shortform,
        "shoot_mode": shoot_mode,
        "videos": [
            {
                "video_id": item.video_id,
                "duration_ms": item.duration_ms,
                "fps": item.fps,
                "frame_count": len(item.keyframes),
                "first": (
                    hashlib.sha256(item.keyframes[0].image_url.encode()).hexdigest()[:16]
                    if item.keyframes
                    else ""
                ),
                "last": (
                    hashlib.sha256(item.keyframes[-1].image_url.encode()).hexdigest()[:16]
                    if item.keyframes
                    else ""
                ),
            }
            for item in contexts
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _renderer_capabilities(settings: Settings | None = None) -> dict[str, Any]:
    runtime_settings = settings or get_settings()
    capabilities = get_reals_registry().llm_capabilities()
    disabled_effects = runtime_settings.editing_disabled_effect_ids_set
    capabilities["effects"] = [
        effect_id for effect_id in capabilities["effects"] if effect_id not in disabled_effects
    ]
    enabled_effects = set(capabilities["effects"])
    capabilities["effect_contracts"] = {
        effect_id: contract
        for effect_id, contract in capabilities["effect_contracts"].items()
        if effect_id in enabled_effects
    }
    capabilities["max_output_duration_sec"] = runtime_settings.editing_max_output_duration_seconds
    capabilities["max_input_videos"] = runtime_settings.editing_max_videos_per_run
    capabilities["timed_effect_window"] = "clip-relative start_ms/end_ms"
    return capabilities


def _requirements(
    source_preparation: dict[str, Any],
    *,
    reduced_structure: bool = False,
) -> list[str]:
    requirements = [
        "clip_order must be consecutive from 1 and timeline_start_ms must be gapless from 0.",
        (
            "Follow reference edit-segment order. A supplied video id may be reused through multiple non-overlapping source ranges."
            if source_preparation.get("strategy") == "INFORMATIONAL_REASSEMBLY"
            else "Preserve ascending shooting_scene_order and use only supplied video ids."
        ),
        "Every source timestamp must be inside that video's duration.",
        "Caption times are absolute timeline milliseconds and must stay inside their clip.",
        "Caption scale must remain 1.0; use an approved style_id for visual emphasis.",
        "Use only renderer capabilities and the video-editing DB editing_rules.",
        "Keep captions at most 40 characters each and at most 8 captions total.",
        "This is promotional video: regular in-video captions are required, not optional. "
        "Create at least 3 regular captions when the timeline has 3 or more clips; otherwise "
        "create one regular caption per clip. The rendered CTA does not count toward this minimum.",
        "The first clip must contain a concise HOOK caption grounded in the verified project "
        "promotion_subject. Use CAPTION_EMPHASIS on at least one item or reveal moment.",
        "Treat editing_context.project_brief as the authoritative copy brief. Carry its confirmed "
        "promotion subject, objective, creative preferences, secondary information, verified user "
        "facts, and recent user wording into captions, CTA, publishing title, and post caption.",
        "Prefer concrete verified details and the user's own concise wording over generic phrases. "
        "When project_brief contains a usable detail, do not replace it with vague copy such as "
        "'특별한 순간' or '매력'. Never invent a price, benefit, review, or store fact.",
        "Use only copy directives scoped to this project. Every phrase in "
        "editing_context.project_brief.copy_directives.verbatim_caption_phrases must appear "
        "unchanged in an in-video caption or the CTA. Never import wording from another project, "
        "session, menu, or store.",
        "Write audience-facing promotional captions, not production notes. Never narrate filming "
        "or editing directions such as close-up, transition, scene setup, clothing change, hand "
        "movement, or showing an item, unless that exact wording is a project-scoped required "
        "verbatim caption phrase.",
        "For a concise first promotional HOOK, prefer motion_id TYPEWRITER so text appears one "
        "Korean character at a time. TYPEWRITER captions must contain at most 18 non-space "
        "characters and allow 80ms per character plus at least 600ms of fully visible hold time.",
        "Use TYPEWRITER on at most 2 captions per video. Prefer POP for short item-reveal captions "
        "and NONE or FADE for ordinary explanatory captions and the final CTA.",
        "Distribute promotional captions across the timeline and align reveal captions to observed "
        "item-appearance or semantic-event evidence. Do not leave the video with only the final CTA.",
        "Use a verified item name only when supplied by project promotion_subject or supported by "
        "the editing context. Otherwise use truthful category copy such as 메뉴, 음료, or 메인 메뉴 "
        "instead of inventing a specific product name.",
        "Publishing title and caption are separate: title is a short hook and caption is the post body.",
        "Publishing title, caption, and video CTA must contain marketing copy only; never put music, upload, platform, or other operational instructions in them.",
        "Return 5 to 20 unique hashtags. Every hashtag must begin with # and contain no whitespace.",
        "Never guess a song title or artist. Use FIXED only for verified metadata; otherwise use SUGGESTED with a concise platform search_keyword derived from the selected trend/template.",
        "Audio start_sec and end_sec must be null until source-song audio matching is available.",
        "Publishing post_note must tell the user how to add music in the platform and, for SUGGESTED audio, include track.search_keyword verbatim.",
        "Match reference-original composition/effect grammar using the frame-exact user evidence; do not copy unsupported content.",
        "Timed effect params start_ms/end_ms are relative to the host clip after speed and must align to analyzed semantic events.",
    ]
    if source_preparation.get("mode") == "MULTI_CUT":
        requirements.append(
            "The final recipe must preserve source-preparation video order and exact trim_in_ms/trim_out_ms boundaries."
        )
    else:
        requirements.append(
            "ONE_TAKE is passthrough for source preparation and must keep the full source duration."
        )
    if reduced_structure:
        requirements.append(
            "The user selected USE_REDUCED_STRUCTURE: return RECIPE, omit unsupported roles, "
            "and use the available videos conservatively in shooting order."
        )
    return requirements


def _is_information_format(video_editing_db: dict[str, Any]) -> bool:
    metadata = video_editing_db.get("recommendation_metadata") or {}
    return str(metadata.get("format_type") or "") == "정보형"


def _is_reduced_structure_revision(revision_action: str | None) -> bool:
    return (revision_action or "").strip().upper() == "USE_REDUCED_STRUCTURE"


def _make_strict_schema(value: Any) -> Any:
    if isinstance(value, list):
        return [_make_strict_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {
        key: _make_strict_schema(item)
        for key, item in value.items()
        if key not in {"default", "title"}
    }
    if result.get("type") == "object" or "properties" in result:
        properties = result.get("properties", {})
        result["required"] = list(properties)
        result["additionalProperties"] = False
    return result


_ModelT = TypeVar("_ModelT", bound=BaseModel)

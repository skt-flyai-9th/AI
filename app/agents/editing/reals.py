from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agents.editing.types import VideoContext
from app.core.config import get_settings
from app.schemas.editing import EditRecipe, EditingVideoInput


class RealsRegistryError(RuntimeError):
    """The renderer registry bundle is missing, corrupt, or internally inconsistent."""


class RealsRegistry:
    """Read the same checksummed registry bundle used by the standalone REALS engine."""

    def __init__(self, registry_dir: str | Path | None = None) -> None:
        configured = registry_dir or get_settings().editing_reals_registry_path
        self.registry_dir = _resolve_registry_dir(Path(configured))
        self.manifest = self._load("manifest")
        self._verify_manifest()
        self.effects = self._load("effect_registry")
        self.edit_policies = self._load("edit_policies")
        self.render_profiles = self._load("render_profiles")
        self.safe_area_profiles = self._load("safe_area_profiles")
        self.audio_mix_policies = self._load("audio_mix_policy")

    def _load(self, name: str) -> dict[str, Any]:
        path = self.registry_dir / f"{name}.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RealsRegistryError(f"REALS registry file is missing: {path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise RealsRegistryError(f"REALS registry file is invalid: {path}") from exc
        if not isinstance(value, dict):
            raise RealsRegistryError(f"REALS registry root must be an object: {path}")
        return value

    def _verify_manifest(self) -> None:
        files = self.manifest.get("files")
        if not isinstance(files, dict):
            raise RealsRegistryError("REALS registry manifest.files is missing.")
        for name, expected in files.items():
            path = self.registry_dir / f"{name}.json"
            try:
                actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                raise RealsRegistryError(f"REALS registry file is missing: {path}") from exc
            if actual != expected:
                raise RealsRegistryError(f"REALS registry checksum mismatch: {path}")

    @property
    def template_bundle_id(self) -> str:
        value = self.manifest.get("template_bundle_id")
        if not isinstance(value, str) or not value:
            raise RealsRegistryError("REALS template_bundle_id is missing.")
        return value

    @property
    def manifest_sha256(self) -> str:
        path = self.registry_dir / "manifest.json"
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    @property
    def creative_effect_ids(self) -> set[str]:
        effects = self.effects.get("effects", {})
        return set(effects) - {"NONE", "SPEED"}

    @property
    def transition_ids(self) -> set[str]:
        return set(self.effects.get("transitions", []))

    def effect_rules(self, effect_id: str) -> dict[str, Any] | None:
        value = self.effects.get("effects", {}).get(effect_id)
        return value if isinstance(value, dict) else None

    def caption_style_ids(self) -> set[str]:
        value = self.effects.get("overlay_types", {}).get("CAPTION", {}).get("style_ids", [])
        return set(value)

    def caption_motion_ids(self) -> set[str]:
        value = self.effects.get("overlay_types", {}).get("CAPTION", {}).get("motion_ids", [])
        return set(value)

    def render_profile(self, profile_id: str) -> dict[str, Any] | None:
        value = self.render_profiles.get("profiles", {}).get(profile_id)
        return value if isinstance(value, dict) else None

    def has_safe_area_profile(self, profile_id: str) -> bool:
        return profile_id in self.safe_area_profiles.get("profiles", {})

    def audio_mix_policy(self, policy_id: str) -> dict[str, Any] | None:
        value = self.audio_mix_policies.get("policies", {}).get(policy_id)
        return value if isinstance(value, dict) else None

    def llm_capabilities(self) -> dict[str, Any]:
        speed = self.effect_rules("SPEED") or {}
        speed_params = speed.get("allowed_params", {}).get("multiplier", {})
        policies = self.edit_policies
        return {
            "registry_bundle_id": self.template_bundle_id,
            "registry_versions": {
                "effects": self.effects.get("effect_registry_version"),
                "edit_policy": policies.get("edit_policy_version"),
            },
            "source_type": "VIDEO_ONLY",
            "speed_range": [speed_params.get("min", 0.5), speed_params.get("max", 2.0)],
            "crop_modes": ["KEEP", "SUBJECT_CENTER", "CENTER_9_16"],
            "transitions": sorted((self.transition_ids - {"NONE"}) | {"CUT"}),
            "effects": sorted(self.creative_effect_ids),
            "caption_positions": ["BOTTOM", "MIDDLE", "TOP"],
            "caption_style_ids": sorted(self.caption_style_ids()),
            "caption_motion_ids": sorted(self.caption_motion_ids()),
            "font_weights": ["REGULAR", "SEMIBOLD", "BOLD"],
            "caption_scale": 1.0,
            "max_caption_chars": policies.get("max_caption_chars", 40),
            "max_captions_per_video": policies.get("max_captions_per_video", 8),
            "original_audio_policy": "REMOVE",
            "bgm_policy": "NONE",
        }


@lru_cache(maxsize=1)
def get_reals_registry() -> RealsRegistry:
    return RealsRegistry()


class RealsRemoteMediaRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: str
    asset_url: str
    sha256: str = ""
    duration_ms: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: float = Field(gt=0)


class RealsPreparedMediaRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: str
    duration_ms: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: float = Field(gt=0)


class RealsAssemblySegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assembly_segment_id: str
    source_file_id: str
    sequence_index: int = Field(ge=1)
    trim_in_ms: int = Field(ge=0)
    trim_out_ms: int = Field(gt=0)


class RealsSourceAssemblyPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_mode: Literal["SOURCE_ASSEMBLY"] = "SOURCE_ASSEMBLY"
    output_file_id: str
    output_profile_id: str = "INTERMEDIATE_VERTICAL_V1"
    flow_preserved: Literal[True] = True
    segments: list[RealsAssemblySegment] = Field(min_length=2)


class RealsEffectApplication(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effect_id: str
    params: dict[str, Any] = Field(default_factory=dict)


class RealsRecipeSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipe_segment_id: str
    produced_segment_id: str
    sequence_index: int = Field(ge=1)
    trim_in_ms: int = Field(ge=0)
    trim_out_ms: int = Field(gt=0)
    speed_multiplier: float = Field(ge=0.5, le=2.0)
    crop_mode: Literal["KEEP", "CENTER_9_16"]
    color_tone: Literal["NATURAL", "WARM", "COOL", "VIVID"] = "NATURAL"
    transition_id: Literal["NONE", "HARD_CUT", "FLASH_WHITE"] = "NONE"
    effects: list[RealsEffectApplication] = Field(default_factory=list)
    actual_video_evidence: str = ""
    flow_preserved: Literal[True] = True


class RealsOverlay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overlay_id: str
    produced_segment_id: str
    overlay_type: Literal["CAPTION"] = "CAPTION"
    text_content: str
    style_id: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    placement_id: Literal["BOTTOM_SAFE", "MID_SAFE", "UPPER_SAFE"]
    motion_id: Literal["NONE", "FADE", "POP", "TYPEWRITER"] = "NONE"
    font_asset_id: Literal["PRETENDARD"] = "PRETENDARD"
    font_weight: Literal["REGULAR", "SEMIBOLD", "BOLD"] = "SEMIBOLD"
    sfx_intent_id: str = ""
    sfx_strength: Literal["LIGHT"] = "LIGHT"
    audio_volume_db: float = -15.0
    actual_video_evidence: str = ""
    system_added: Literal[True] = True


class RealsEditRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipe_id: str
    recipe_version: int = Field(ge=1)
    recipe_schema_version: Literal["video-edit-decision-1.0"] = "video-edit-decision-1.0"
    produced_video_id: str
    flow_preserved: Literal[True] = True
    segments: list[RealsRecipeSegment] = Field(min_length=1)
    overlays: list[RealsOverlay] = Field(default_factory=list)
    original_audio_policy: Literal["REMOVE"] = "REMOVE"
    bgm_policy: Literal["NONE"] = "NONE"
    final_audio_policy: Literal["SILENT"] = "SILENT"
    font_asset_id: Literal["PRETENDARD"] = "PRETENDARD"
    render_profile_id: str = "INSTAGRAM_REELS_V1"
    safe_area_profile_id: str = "INSTAGRAM_REELS_2026_V1"
    audio_mix_policy_id: Literal["SILENT_V1"] = "SILENT_V1"
    thumbnail_source_ms: int = Field(default=0, ge=0)


class RealsFinalRenderPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_mode: Literal["FINAL_RENDER"] = "FINAL_RENDER"
    produced_video: RealsPreparedMediaRef
    source_mode: Literal["ONE_TAKE_PASSTHROUGH", "MULTI_CUT_ASSEMBLED"]
    edit_recipe: RealsEditRecipe
    template_bundle_id: str


class RealsRenderJobRequest(BaseModel):
    """Network contract consumed by the service that hosts the REALS engine.

    The service resolves remote assets to local MediaFileRef paths. For multi-cut
    input it executes source_assembly first, then creates the engine-native
    FinalRenderRequest represented by final_render.
    """

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["reals-render-job-1.0"] = "reals-render-job-1.0"
    job_id: str
    idempotency_key: str
    registry_manifest_sha256: str
    source_assets: list[RealsRemoteMediaRef] = Field(min_length=1)
    source_assembly: RealsSourceAssemblyPlan | None
    final_render: RealsFinalRenderPlan


class RealsRecipeAdapter:
    """Translate an app EditRecipe into the deterministic REALS service contract."""

    def __init__(self, registry: RealsRegistry | None = None) -> None:
        self.registry = registry or get_reals_registry()

    def build_request(
        self,
        *,
        run_id: str,
        recipe: EditRecipe,
        videos: list[EditingVideoInput],
        video_contexts: list[VideoContext],
        video_editing_db: dict[str, Any],
    ) -> RealsRenderJobRequest:
        videos_by_id = {video.video_id: video for video in videos}
        contexts_by_id = {context.video_id: context for context in video_contexts}
        used_ids = list(dict.fromkeys(clip.video_id for clip in recipe.timeline))
        missing = [video_id for video_id in used_ids if video_id not in videos_by_id]
        missing += [video_id for video_id in used_ids if video_id not in contexts_by_id]
        if missing:
            raise ValueError(f"Missing renderer source metadata: {sorted(set(missing))}")

        source_assets = [
            RealsRemoteMediaRef(
                file_id=video_id,
                asset_url=videos_by_id[video_id].footage_url,
                duration_ms=contexts_by_id[video_id].duration_ms,
                width=contexts_by_id[video_id].width,
                height=contexts_by_id[video_id].height,
                fps=contexts_by_id[video_id].fps,
            )
            for video_id in used_ids
        ]

        rules = video_editing_db.get("editing_rules") or {}
        render_profile_id = str(rules.get("render_profile_id") or "INSTAGRAM_REELS_V1")
        safe_area_profile_id = str(
            rules.get("safe_area_profile_id") or "INSTAGRAM_REELS_2026_V1"
        )
        render_profile = self.registry.render_profile(render_profile_id)
        if render_profile is None:
            raise ValueError(f"Unknown REALS render profile: {render_profile_id}")
        if not self.registry.has_safe_area_profile(safe_area_profile_id):
            raise ValueError(f"Unknown REALS safe-area profile: {safe_area_profile_id}")
        if self.registry.audio_mix_policy("SILENT_V1") is None:
            raise ValueError("Missing REALS audio policy: SILENT_V1")
        assembly_profile_id = str(
            rules.get("assembly_profile_id") or "INTERMEDIATE_VERTICAL_V1"
        )
        assembly_profile = self.registry.render_profile(assembly_profile_id)
        if assembly_profile is None:
            raise ValueError(f"Unknown REALS assembly profile: {assembly_profile_id}")

        produced_video_id = f"{run_id}_produced"
        multi_cut = len(recipe.timeline) > 1
        assembly: RealsSourceAssemblyPlan | None = None
        produced_duration_ms: int
        produced_ranges: list[tuple[int, int]] = []

        if multi_cut:
            assembly_segments: list[RealsAssemblySegment] = []
            produced_cursor = 0
            for clip in recipe.timeline:
                source_duration = clip.source_end_ms - clip.source_start_ms
                produced_ranges.append((produced_cursor, produced_cursor + source_duration))
                assembly_segments.append(
                    RealsAssemblySegment(
                        assembly_segment_id=f"asm_{clip.clip_order:03d}",
                        source_file_id=clip.video_id,
                        sequence_index=clip.clip_order,
                        trim_in_ms=clip.source_start_ms,
                        trim_out_ms=clip.source_end_ms,
                    )
                )
                produced_cursor += source_duration
            produced_duration_ms = produced_cursor
            assembly = RealsSourceAssemblyPlan(
                output_file_id=produced_video_id,
                output_profile_id=assembly_profile_id,
                segments=assembly_segments,
            )
            source_mode = "MULTI_CUT_ASSEMBLED"
            produced_width = int(assembly_profile["width"])
            produced_height = int(assembly_profile["height"])
            produced_fps = float(assembly_profile["fps"])
        else:
            clip = recipe.timeline[0]
            context = contexts_by_id[clip.video_id]
            produced_video_id = clip.video_id
            produced_duration_ms = context.duration_ms
            produced_ranges = [(clip.source_start_ms, clip.source_end_ms)]
            source_mode = "ONE_TAKE_PASSTHROUGH"
            produced_width = context.width
            produced_height = context.height
            produced_fps = context.fps

        segments: list[RealsRecipeSegment] = []
        overlays: list[RealsOverlay] = []
        for index, (clip, produced_range) in enumerate(
            zip(recipe.timeline, produced_ranges, strict=True)
        ):
            produced_segment_id = f"ps_{clip.clip_order:03d}"
            color_tone = "NATURAL"
            effects: list[RealsEffectApplication] = []
            for effect in clip.effects:
                params = effect.params.model_dump(exclude_none=True)
                if effect.effect_id == "COLOR_TONE":
                    color_tone = str(params.get("tone") or "NATURAL")
                    continue
                effects.append(RealsEffectApplication(effect_id=effect.effect_id, params=params))

            transition = clip.transition_in
            if transition is None and index > 0:
                transition = recipe.timeline[index - 1].transition_out
            transition_id = _to_reals_transition(transition) if index > 0 else "NONE"
            segments.append(
                RealsRecipeSegment(
                    recipe_segment_id=f"rs_{clip.clip_order:03d}",
                    produced_segment_id=produced_segment_id,
                    sequence_index=clip.clip_order,
                    trim_in_ms=produced_range[0],
                    trim_out_ms=produced_range[1],
                    speed_multiplier=clip.speed,
                    crop_mode=_to_reals_crop(clip.crop_mode),
                    color_tone=color_tone,
                    transition_id=transition_id,
                    effects=effects,
                    actual_video_evidence=f"source_video_id={clip.video_id}",
                )
            )
            if clip.caption is not None:
                caption = clip.caption
                overlays.append(
                    RealsOverlay(
                        overlay_id=f"ov_caption_{clip.clip_order:03d}",
                        produced_segment_id=produced_segment_id,
                        text_content=caption.text,
                        style_id=caption.style_id,
                        start_ms=_output_to_produced_ms(
                            clip.timeline_start_ms,
                            caption.start_ms,
                            clip.speed,
                            produced_range[0],
                        ),
                        end_ms=_output_to_produced_ms(
                            clip.timeline_start_ms,
                            caption.end_ms,
                            clip.speed,
                            produced_range[0],
                        ),
                        placement_id=_to_reals_placement(caption.position),
                        motion_id=caption.motion_id,
                        font_weight=caption.font_weight,
                        actual_video_evidence=f"source_video_id={clip.video_id}",
                    )
                )

        last_clip = recipe.timeline[-1]
        last_range = produced_ranges[-1]
        last_output_end = last_clip.timeline_start_ms + (
            last_clip.source_end_ms - last_clip.source_start_ms
        ) / last_clip.speed
        cta_output_start = max(last_clip.timeline_start_ms, int(last_output_end) - 2500)
        overlays.append(
            RealsOverlay(
                overlay_id="ov_cta",
                produced_segment_id=f"ps_{last_clip.clip_order:03d}",
                text_content=recipe.cta.text,
                style_id="CTA_BOX",
                start_ms=_output_to_produced_ms(
                    last_clip.timeline_start_ms,
                    cta_output_start,
                    last_clip.speed,
                    last_range[0],
                ),
                end_ms=last_range[1],
                placement_id="BOTTOM_SAFE",
                motion_id="FADE",
                font_weight="BOLD",
                actual_video_evidence=f"source_video_id={last_clip.video_id}",
            )
        )

        reals_recipe = RealsEditRecipe(
            recipe_id=f"{run_id}_recipe",
            recipe_version=recipe.recipe_version,
            produced_video_id=produced_video_id,
            segments=segments,
            overlays=overlays,
            render_profile_id=render_profile_id,
            safe_area_profile_id=safe_area_profile_id,
        )
        digest_source = {
            "run_id": run_id,
            "recipe": recipe.model_dump(mode="json"),
            "sources": [item.model_dump(mode="json") for item in source_assets],
            "bundle": self.registry.template_bundle_id,
            "registry_manifest": self.registry.manifest_sha256,
        }
        digest = hashlib.sha256(
            json.dumps(digest_source, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return RealsRenderJobRequest(
            job_id=run_id,
            idempotency_key=f"editing:{run_id}:{digest}",
            registry_manifest_sha256=self.registry.manifest_sha256,
            source_assets=source_assets,
            source_assembly=assembly,
            final_render=RealsFinalRenderPlan(
                produced_video=RealsPreparedMediaRef(
                    file_id=produced_video_id,
                    duration_ms=produced_duration_ms,
                    width=produced_width,
                    height=produced_height,
                    fps=produced_fps,
                ),
                source_mode=source_mode,
                edit_recipe=reals_recipe,
                template_bundle_id=self.registry.template_bundle_id,
            ),
        )


def _resolve_registry_dir(configured: Path) -> Path:
    if configured.is_absolute():
        return configured
    repository_root = Path(__file__).resolve().parents[3]
    candidates = (Path.cwd() / configured, repository_root / configured)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[-1]


def _to_reals_transition(value: str | None) -> str:
    if value in {None, "CUT"}:
        return "HARD_CUT" if value == "CUT" else "NONE"
    return value


def _to_reals_crop(value: str) -> str:
    return "KEEP" if value == "KEEP" else "CENTER_9_16"


def _to_reals_placement(value: str) -> str:
    return {"BOTTOM": "BOTTOM_SAFE", "MIDDLE": "MID_SAFE", "TOP": "UPPER_SAFE"}[value]


def _output_to_produced_ms(
    clip_output_start_ms: int,
    output_timestamp_ms: int,
    speed: float,
    produced_segment_start_ms: int,
) -> int:
    return int(round(produced_segment_start_ms + (output_timestamp_ms - clip_output_start_ms) * speed))

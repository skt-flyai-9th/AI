"""Recipe Validator — render-blocking deterministic validation."""
from __future__ import annotations
import unicodedata

from fontTools.ttLib import TTFont

from .contracts import (
    EditRecipe,
    FinalAudioPolicy,
    MediaFileRef,
    MotionId,
    OverlayType,
    QcCheck,
    QcReport,
    QcStatus,
)
from .registries import Registries, RegistryError


class ValidationError(Exception):
    def __init__(self, failures: list[str]):
        self.failures = failures
        super().__init__("recipe 검증 실패:\n- " + "\n- ".join(failures))


_GLYPH_CACHE: dict[str, set[int]] = {}


def _cmap(font_path: str) -> set[int]:
    if font_path not in _GLYPH_CACHE:
        _GLYPH_CACHE[font_path] = set(TTFont(font_path).getBestCmap().keys())
    return _GLYPH_CACHE[font_path]


def validate_recipe(recipe: EditRecipe, produced: MediaFileRef, reg: Registries) -> QcReport:
    fails: list[str] = []
    warns: list[str] = []
    pol = reg.edit_policies

    if not recipe.flow_preserved:
        fails.append("flow_preserved=false 는 허용되지 않음")
    seq = [s.sequence_index for s in recipe.segments]
    if seq != sorted(seq) or len(set(seq)) != len(seq):
        fails.append(f"sequence_index 단조 증가 위반: {seq}")
    if not recipe.segments:
        fails.append("segments 비어 있음")

    try:
        amix = reg.audio_policy(recipe.audio_mix_policy_id)
        if recipe.final_audio_policy.value != amix["final_audio_policy"]:
            fails.append(
                f"final_audio_policy({recipe.final_audio_policy.value})가 "
                f"mix policy({amix['final_audio_policy']})와 불일치"
            )
    except RegistryError as e:
        fails.append(str(e))
        amix = {
            "max_sfx_per_video": 0,
            "min_sfx_gap_ms": 0,
            "sfx_volume_db_range": {"min": -30, "max": -6},
        }

    dur = produced.duration_ms
    min_cut = pol["min_cut_duration_ms"]
    total_out_ms = 0.0
    for s in recipe.segments:
        if s.trim_in_ms < 0 or s.trim_out_ms > dur:
            fails.append(
                f"{s.recipe_segment_id}: 트림 [{s.trim_in_ms},{s.trim_out_ms}]가 "
                f"영상 범위(0..{dur}) 밖"
            )
        try:
            reg.validate_effect("SPEED", {"multiplier": s.speed_multiplier})
        except RegistryError as e:
            fails.append(str(e))
        cut_ms = (s.trim_out_ms - s.trim_in_ms) / max(s.speed_multiplier, 0.01)
        if cut_ms < min_cut:
            fails.append(f"{s.recipe_segment_id}: 컷 {cut_ms:.0f}ms < 최소 {min_cut}ms")
        total_out_ms += cut_ms
        allowed_tr = reg.effect.get("transitions", ["NONE", "HARD_CUT"])
        if s.transition_id.value not in allowed_tr:
            fails.append(f"{s.recipe_segment_id}: 미등록 transition {s.transition_id.value}")
        for eff in s.effects:
            try:
                reg.validate_effect(eff.effect_id, eff.params)
            except RegistryError as e:
                fails.append(str(e))
            start_ms = eff.params.get("start_ms")
            end_ms = eff.params.get("end_ms")
            if start_ms is not None or end_ms is not None:
                if start_ms is None or end_ms is None:
                    fails.append(f"{s.recipe_segment_id}/{eff.effect_id}: effect window incomplete")
                elif start_ms < 0 or end_ms <= start_ms or end_ms > cut_ms + 1:
                    fails.append(
                        f"{s.recipe_segment_id}/{eff.effect_id}: effect window "
                        f"[{start_ms},{end_ms}] outside clip output 0..{cut_ms:.0f}ms"
                    )

    try:
        rp = reg.render_profile(recipe.render_profile_id)
        if total_out_ms / 1000 > rp["max_duration_sec"]:
            fails.append(f"출력 {total_out_ms/1000:.1f}s > 허용 {rp['max_duration_sec']}s")
    except RegistryError as e:
        fails.append(str(e))

    seg_by_id = {s.produced_segment_id: s for s in recipe.segments}
    captions = [o for o in recipe.overlays if o.overlay_type != OverlayType.SFX]
    sfx = [o for o in recipe.overlays if o.overlay_type == OverlayType.SFX]

    if len(captions) > pol["max_captions_per_video"]:
        fails.append(f"자막 {len(captions)}개 > 최대 {pol['max_captions_per_video']}")

    for o in recipe.overlays:
        host = seg_by_id.get(o.produced_segment_id)
        if host is None:
            fails.append(
                f"{o.overlay_id}: 존재하지 않는 produced_segment {o.produced_segment_id} 참조"
            )
            continue
        lo = max(o.start_ms, host.trim_in_ms)
        hi = min(o.end_ms, host.trim_out_ms)
        if hi - lo <= 0:
            fails.append(
                f"{o.overlay_id}: 노출창 [{o.start_ms},{o.end_ms}]가 세그먼트 "
                f"사용구간 [{host.trim_in_ms},{host.trim_out_ms}]와 교집합 없음"
            )
        if o.overlay_type != OverlayType.SFX:
            if o.font_asset_id != "PRETENDARD":
                fails.append(
                    f"{o.overlay_id}: 승인 폰트는 PRETENDARD뿐 ({o.font_asset_id})"
                )
            if len(o.text_content) > pol["max_caption_chars"]:
                fails.append(
                    f"{o.overlay_id}: 문구 {len(o.text_content)}자 > {pol['max_caption_chars']}자"
                )
            if o.style_id not in reg.style_ids_for(o.overlay_type.value):
                fails.append(f"{o.overlay_id}: 미등록 style {o.style_id}")
            if o.motion_id.value not in reg.motion_ids_for(o.overlay_type.value):
                fails.append(f"{o.overlay_id}: 미등록 motion {o.motion_id.value}")
            if o.motion_id == MotionId.TYPEWRITER:
                unit_count = sum(
                    1
                    for ch in unicodedata.normalize("NFC", o.text_content)
                    if not ch.isspace()
                )
                if unit_count > 18:
                    fails.append(f"{o.overlay_id}: TYPEWRITER 문구 {unit_count}자 > 최대 18자")
                required_ms = max(0, unit_count - 1) * 80 + 600
                if o.end_ms - o.start_ms < required_ms:
                    fails.append(
                        f"{o.overlay_id}: TYPEWRITER 노출시간 부족 "
                        f"({o.end_ms-o.start_ms}ms < {required_ms}ms)"
                    )
            try:
                f = reg.resolve_font(o.font_asset_id, o.font_weight.value)
                missing = [
                    ch
                    for ch in o.text_content
                    if not ch.isspace() and ord(ch) not in _cmap(f["abs_path"])
                ]
                if missing:
                    fails.append(
                        f"{o.overlay_id}: 폰트 미지원 글리프 {missing!r} — "
                        "OS 폰트 fallback 금지, 문구 수정 필요"
                    )
            except RegistryError as e:
                fails.append(str(e))
        else:
            if o.sfx_intent_id not in reg.sfx_intent_ids():
                fails.append(f"{o.overlay_id}: 미등록 SFX intent {o.sfx_intent_id}")
            rng = amix["sfx_volume_db_range"]
            if not (rng["min"] <= o.audio_volume_db <= rng["max"]):
                fails.append(f"{o.overlay_id}: 볼륨 {o.audio_volume_db}dB 범위 밖 {rng}")

    if recipe.final_audio_policy == FinalAudioPolicy.SILENT and sfx:
        fails.append("SILENT 정책인데 SFX 오버레이 존재")
    if len(sfx) > amix["max_sfx_per_video"]:
        fails.append(f"SFX {len(sfx)}개 > 최대 {amix['max_sfx_per_video']}")
    starts = sorted(o.start_ms for o in sfx)
    for a, b in zip(starts, starts[1:]):
        if b - a < amix["min_sfx_gap_ms"]:
            fails.append(f"SFX 간격 {b-a}ms < 최소 {amix['min_sfx_gap_ms']}ms")

    try:
        reg.resolve_font(recipe.font_asset_id, "REGULAR")
    except RegistryError as e:
        fails.append(str(e))

    if fails:
        raise ValidationError(fails)
    checks = [
        QcCheck(
            check_id="recipe_validation",
            status=QcStatus.PASS,
            detail=(
                f"segments={len(recipe.segments)} overlays={len(recipe.overlays)} "
                f"expected_out={total_out_ms:.0f}ms"
            ),
        )
    ]
    checks += [
        QcCheck(check_id="recipe_warn", status=QcStatus.WARN, detail=w) for w in warns
    ]
    return QcReport.summarize(checks)


def expected_duration_ms(recipe: EditRecipe) -> int:
    return int(
        round(
            sum(
                (s.trim_out_ms - s.trim_in_ms) / s.speed_multiplier
                for s in recipe.segments
            )
        )
    )

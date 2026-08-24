"""Recipe Validator — 렌더 전 규칙 검증 (구현 문서 15.1·15.2).

검증 실패 = FINAL_RENDER 진입 금지. Validator는 의미 판단을 하지 않고
정책·범위·참조·순서만 본다.
"""
from __future__ import annotations
import pathlib

from fontTools.ttLib import TTFont

from .contracts import (EditRecipe, FinalAudioPolicy, MediaFileRef, OverlayType,
                        QcCheck, QcReport, QcStatus)
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

    # 1. 플로우 보존 — 협의 확정: 순서는 이미 결정, 재배열 금지
    if not recipe.flow_preserved:
        fails.append("flow_preserved=false 는 허용되지 않음")
    seq = [s.sequence_index for s in recipe.segments]
    if seq != sorted(seq) or len(set(seq)) != len(seq):
        fails.append(f"sequence_index 단조 증가 위반: {seq}")
    if not recipe.segments:
        fails.append("segments 비어 있음")

    # 2. 오디오 정책 — 협의 확정: 원음 REMOVE / BGM NONE / SILENT|SFX_ONLY
    try:
        amix = reg.audio_policy(recipe.audio_mix_policy_id)
        if recipe.final_audio_policy.value != amix["final_audio_policy"]:
            fails.append(f"final_audio_policy({recipe.final_audio_policy.value})가 "
                         f"mix policy({amix['final_audio_policy']})와 불일치")
    except RegistryError as e:
        fails.append(str(e)); amix = {"max_sfx_per_video": 0, "min_sfx_gap_ms": 0,
                                      "sfx_volume_db_range": {"min": -30, "max": -6}}

    # 3. 세그먼트: 트림 범위·최소 길이·speed·effect
    dur = produced.duration_ms
    min_cut = pol["min_cut_duration_ms"]
    total_out_ms = 0.0
    for s in recipe.segments:
        if s.trim_in_ms < 0 or s.trim_out_ms > dur:
            fails.append(f"{s.recipe_segment_id}: 트림 [{s.trim_in_ms},{s.trim_out_ms}]가 "
                         f"영상 범위(0..{dur}) 밖")
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

    # 4. 전체 길이 vs render profile
    try:
        rp = reg.render_profile(recipe.render_profile_id)
        if total_out_ms / 1000 > rp["max_duration_sec"]:
            fails.append(f"출력 {total_out_ms/1000:.1f}s > 허용 {rp['max_duration_sec']}s")
    except RegistryError as e:
        fails.append(str(e))

    # 5. 오버레이: 참조·시간창·style·글리프·폰트
    seg_by_id = {s.produced_segment_id: s for s in recipe.segments}
    captions = [o for o in recipe.overlays if o.overlay_type != OverlayType.SFX]
    sfx = [o for o in recipe.overlays if o.overlay_type == OverlayType.SFX]

    if len(captions) > pol["max_captions_per_video"]:
        fails.append(f"자막 {len(captions)}개 > 최대 {pol['max_captions_per_video']}")

    for o in recipe.overlays:
        host = seg_by_id.get(o.produced_segment_id)
        if host is None:
            fails.append(f"{o.overlay_id}: 존재하지 않는 produced_segment "
                         f"{o.produced_segment_id} 참조")
            continue
        lo = max(o.start_ms, host.trim_in_ms)
        hi = min(o.end_ms, host.trim_out_ms)
        if hi - lo <= 0:
            fails.append(f"{o.overlay_id}: 노출창 [{o.start_ms},{o.end_ms}]가 세그먼트 "
                         f"사용구간 [{host.trim_in_ms},{host.trim_out_ms}]와 교집합 없음")
        if o.overlay_type != OverlayType.SFX:
            if o.font_asset_id != "PRETENDARD":
                fails.append(f"{o.overlay_id}: 승인 폰트는 PRETENDARD뿐 ({o.font_asset_id})")
            if len(o.text_content) > pol["max_caption_chars"]:
                fails.append(f"{o.overlay_id}: 문구 {len(o.text_content)}자 > "
                             f"{pol['max_caption_chars']}자")
            if o.style_id not in reg.style_ids_for(o.overlay_type.value):
                fails.append(f"{o.overlay_id}: 미등록 style {o.style_id}")
            try:
                f = reg.resolve_font(o.font_asset_id, o.font_weight.value)
                missing = [ch for ch in o.text_content
                           if not ch.isspace() and ord(ch) not in _cmap(f["abs_path"])]
                if missing:
                    fails.append(f"{o.overlay_id}: 폰트 미지원 글리프 {missing!r} — "
                                 "OS 폰트 fallback 금지, 문구 수정 필요")
            except RegistryError as e:
                fails.append(str(e))
        else:
            if o.sfx_intent_id not in reg.sfx_intent_ids():
                fails.append(f"{o.overlay_id}: 미등록 SFX intent {o.sfx_intent_id}")
            rng = amix["sfx_volume_db_range"]
            if not (rng["min"] <= o.audio_volume_db <= rng["max"]):
                fails.append(f"{o.overlay_id}: 볼륨 {o.audio_volume_db}dB 범위 밖 {rng}")

    # 6. SFX 정책: 개수·간격·SILENT와의 모순
    if recipe.final_audio_policy == FinalAudioPolicy.SILENT and sfx:
        fails.append("SILENT 정책인데 SFX 오버레이 존재")
    if len(sfx) > amix["max_sfx_per_video"]:
        fails.append(f"SFX {len(sfx)}개 > 최대 {amix['max_sfx_per_video']}")
    starts = sorted(o.start_ms for o in sfx)
    for a, b in zip(starts, starts[1:]):
        if b - a < amix["min_sfx_gap_ms"]:
            fails.append(f"SFX 간격 {b-a}ms < 최소 {amix['min_sfx_gap_ms']}ms")

    # 7. 폰트 자산 무결성 (문구 없어도 레시피 폰트는 존재해야 함)
    try:
        reg.resolve_font(recipe.font_asset_id, "REGULAR")
    except RegistryError as e:
        fails.append(str(e))

    if fails:
        raise ValidationError(fails)
    checks = [QcCheck(check_id="recipe_validation", status=QcStatus.PASS,
                      detail=f"segments={len(recipe.segments)} overlays={len(recipe.overlays)} "
                             f"expected_out={total_out_ms:.0f}ms")]
    checks += [QcCheck(check_id="recipe_warn", status=QcStatus.WARN, detail=w) for w in warns]
    return QcReport.summarize(checks)


def expected_duration_ms(recipe: EditRecipe) -> int:
    return int(round(sum((s.trim_out_ms - s.trim_in_ms) / s.speed_multiplier
                         for s in recipe.segments)))

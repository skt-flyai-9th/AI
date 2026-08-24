"""Subtitle Layout Engine — 실제 폰트 메트릭 기반 배치 (구현 문서 18).

원칙:
- 최종 x/y 좌표는 GPT가 아니라 이 엔진이 만든다.
- 자막은 한 Scene(오버레이 노출창) 동안 위치가 고정된다.
- Safe Area blocked region은 하드 제외, Avoid Map은 우선순위 가중 회피.
- 줄바꿈·크기는 Pretendard 실제 메트릭(PIL freetype)으로 계산한다.
"""
from __future__ import annotations
from dataclasses import dataclass, field

from PIL import ImageFont

from .contracts import AvoidMap, MotionId, Overlay, OverlayType, PlacementId
from .registries import Registries

# 스타일 정의: 크기·색·테두리 (ASS 좌표계 = PlayRes 1080x1920 픽셀)
STYLE_SPECS = {
    # 릴스 네이티브 톤: 크고 두껍게, 부드러운 외곽선(\blur), 명확한 대비
    "CAPTION":          {"size": 68, "primary": "&H00FFFFFF", "outline_c": "&H00000000",
                         "outline": 5, "shadow": 0, "border_style": 1},
    "CAPTION_EMPHASIS": {"size": 80, "primary": "&H0024E8FF", "outline_c": "&H00000000",
                         "outline": 6, "shadow": 0, "border_style": 1},
    "HOOK":             {"size": 92, "primary": "&H00FFFFFF", "outline_c": "&H00000000",
                         "outline": 7, "shadow": 0, "border_style": 1, "tilt": -4},
    "CTA_BOX":          {"size": 64, "primary": "&H00FFFFFF", "outline_c": "&H6E101010",
                         "outline": 16, "shadow": 0, "border_style": 3},
    "TEXT_2D":          {"size": 88, "primary": "&H00FFFFFF", "outline_c": "&H00000000",
                         "outline": 6, "shadow": 1, "border_style": 1},
}
LINE_SPACING = 1.18
HARD_PRIORITY = 90     # 이 우선순위 이상(FACE·HANDS·FOOD·TEXT)은 강하게 회피
SOFT_WEIGHT = 0.15     # 그 미만(PERSON_BODY 등)은 약한 선호 신호로만
DIST_WEIGHT = 30.0     # 편집 관례상 선호 위치에서 멀어지는 비용 (px당)


@dataclass
class PlacedOverlay:
    overlay: Overlay
    out_start_ms: int          # 출력 타임라인 기준 (렌더러가 매핑해 넣음)
    out_end_ms: int
    x: int = 0                 # \pos 중심 좌표
    y: int = 0
    font_px: int = 64
    lines: list[str] = field(default_factory=list)
    band_label: str = ""


class LayoutError(Exception):
    pass


def _measure(font: ImageFont.FreeTypeFont, text: str) -> float:
    return font.getlength(text)


def _wrap(font: ImageFont.FreeTypeFont, text: str, max_w: float) -> list[str] | None:
    """어절 우선, 넘치면 문자 단위(CJK) 분할. 실패 시 None."""
    words = text.split(" ")
    lines: list[str] = []
    cur = ""
    for w in words:
        cand = (cur + " " + w).strip()
        if _measure(font, cand) <= max_w:
            cur = cand
            continue
        if cur:
            lines.append(cur)
            cur = ""
        if _measure(font, w) <= max_w:
            cur = w
            continue
        # 한 어절이 폭 초과 → 문자 단위
        piece = ""
        for ch in w:
            if _measure(font, piece + ch) <= max_w:
                piece += ch
            else:
                if piece:
                    lines.append(piece)
                piece = ch
        cur = piece
    if cur:
        lines.append(cur)
    return lines or None


def _overlap_penalty(bx, by, bw, bh, avoid: AvoidMap, t0: int, t1: int) -> float:
    """하드(≥90)와 소프트(<90) 페널티 분리.

    전신 댄스 영상에서는 PERSON_BODY가 화면 대부분을 덮는다. 이를 하드로
    취급하면 배치 최적화가 '몸 픽셀 수'에 지배되어 편집 관례(하단 자막)가
    무너진다. 실제 릴스 자막은 몸통 위에 얹히는 것이 정상이며, 반드시
    피해야 하는 것은 얼굴·손·음식·기존 글자다.
    """
    pen = 0.0
    for r in avoid.regions:
        if r.end_ms <= t0 or r.start_ms >= t1:
            continue
        ix = max(0, min(bx + bw, r.x + r.w) - max(bx, r.x))
        iy = max(0, min(by + bh, r.y + r.h) - max(by, r.y))
        if ix * iy == 0:
            continue
        w = (r.priority / 100.0) if r.priority >= HARD_PRIORITY else \
            (r.priority / 100.0) * SOFT_WEIGHT
        pen += (ix * iy) * w
    return pen


def layout_overlay(o: Overlay, out_start: int, out_end: int, reg: Registries,
                   safe_profile: dict, avoid: AvoidMap) -> PlacedOverlay:
    if o.overlay_type == OverlayType.SFX:
        raise LayoutError("SFX는 레이아웃 대상이 아님")
    pol = reg.edit_policies
    spec = STYLE_SPECS[o.style_id]
    fmeta = reg.resolve_font(o.font_asset_id, o.font_weight.value)

    canvas_w = safe_profile["canvas"]["width"]
    canvas_h = safe_profile["canvas"]["height"]
    blocked = safe_profile["blocked_regions"]
    top_ui = next((b for b in blocked if b["id"] == "TOP_UI"), {"y": 0, "h": 0})
    bottom = next((b for b in blocked if b["id"] == "BOTTOM_CAPTION"),
                  {"y": canvas_h, "h": 0})
    right = next((b for b in blocked if b["id"] == "RIGHT_ACTIONS"), None)
    margin = pol["caption_margin_px"]

    # 연속 y-탐색: 3개 고정 밴드 대신 허용 구간 전체를 40px 간격으로 훑는다.
    # 편집 관례 선호점: CAPTION/CTA는 하단, TEXT_2D/HOOK은 상단 1/3.
    y_top = top_ui["y"] + top_ui["h"] + 90
    y_bot = bottom["y"] - 110
    pref_y = (y_top + 120 if o.overlay_type == OverlayType.TEXT_2D
              or o.style_id == "HOOK" else y_bot - 60)
    if o.placement_id == PlacementId.UPPER_SAFE:
        pref_y = y_top + 120
    elif o.placement_id == PlacementId.MID_SAFE:
        pref_y = int(canvas_h * 0.58)
    elif o.placement_id == PlacementId.BOTTOM_SAFE:
        pref_y = y_bot - 60

    def band_label(cy):
        rel = (cy - y_top) / max(y_bot - y_top, 1)
        return "UPPER_SAFE" if rel < 0.33 else ("MID_SAFE" if rel < 0.66
                                                else "BOTTOM_SAFE")

    best = None
    size = spec["size"]
    while size >= pol["min_font_px"]:
        font = ImageFont.truetype(fmeta["abs_path"], size)
        line_h = int(size * LINE_SPACING)
        for cy in range(y_top, y_bot + 1, 40):
            label = band_label(cy)
            # 이 밴드가 RIGHT_ACTIONS와 세로로 겹치면 사용 가능 폭 축소
            max_w = canvas_w - 2 * margin
            x_center = canvas_w // 2
            if right and not (cy + 200 < right["y"] or cy - 200 > right["y"] + right["h"]):
                usable_r = right["x"] - margin
                max_w = usable_r - margin
                x_center = (margin + usable_r) // 2
            lines = _wrap(font, o.text_content, max_w)
            if not lines or len(lines) > pol["max_caption_lines"]:
                continue
            bw = max(_measure(font, ln) for ln in lines)
            bh = line_h * len(lines)
            bx, by = int(x_center - bw / 2), int(cy - bh / 2)
            # Safe Area 하드 제외
            hard = False
            for b in blocked:
                ix = max(0, min(bx + bw, b["x"] + b["w"]) - max(bx, b["x"]))
                iy = max(0, min(by + bh, b["y"] + b["h"]) - max(by, b["y"]))
                if ix * iy > 0:
                    hard = True
                    break
            if hard or by < 0 or by + bh > canvas_h:
                continue
            pen = _overlap_penalty(bx, by, int(bw), bh, avoid, out_start, out_end)
            score = pen + DIST_WEIGHT * abs(cy - pref_y)
            if best is None or score < best[0]:
                best = (score, label, x_center, cy, size, lines)
        # 하드 회피 성공(순수 거리 비용뿐)이면 현재 크기 유지하고 종료
        if best is not None and best[0] <= DIST_WEIGHT * (y_bot - y_top):
            break
        size -= 8
    if best is None:
        raise LayoutError(f"{o.overlay_id}: 배치 가능한 밴드 없음 — 문구 축소 필요")
    _, label, x, cy, fsize, lines = best
    return PlacedOverlay(overlay=o, out_start_ms=out_start, out_end_ms=out_end,
                         x=x, y=cy, font_px=fsize, lines=lines, band_label=label)


# ── ASS 생성 ─────────────────────────────────────────────────────────
def _ts(ms: int) -> str:
    cs = int(round(ms / 10))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def build_ass(placed: list[PlacedOverlay], reg: Registries, canvas=(1080, 1920)) -> str:
    used_styles = {}
    for p in placed:
        o = p.overlay
        fmeta = reg.resolve_font(o.font_asset_id, o.font_weight.value)
        used_styles[o.style_id] = (fmeta["ass_family"], fmeta["ass_bold"])

    head = [
        "[Script Info]", "ScriptType: v4.00+",
        f"PlayResX: {canvas[0]}", f"PlayResY: {canvas[1]}",
        "ScaledBorderAndShadow: yes", "WrapStyle: 2", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
    ]
    for sid, (family, bold) in used_styles.items():
        sp = STYLE_SPECS[sid]
        head.append(
            f"Style: {sid},{family},{sp['size']},{sp['primary']},&H000000FF,"
            f"{sp['outline_c']},{sp['outline_c']},{bold},0,0,0,100,100,0,0,"
            f"{sp['border_style']},{sp['outline']},{sp['shadow']},5,0,0,0,1")
    head += ["", "[Events]",
             "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]

    events = []
    for p in placed:
        o = p.overlay
        tags = [f"\\pos({p.x},{p.y})", "\\blur0.7"]
        spec2 = STYLE_SPECS[o.style_id]
        if p.font_px != spec2["size"]:
            tags.append(f"\\fs{p.font_px}")
        if spec2.get("tilt"):
            tags.append(f"\\frz{(360 + spec2['tilt']) % 360}")
        if o.motion_id == MotionId.POP:
            # 오버슈트 팝: 74% → 108% → 100%
            tags.append("\\fscx74\\fscy74"
                        "\\t(0,100,\\fscx108\\fscy108)"
                        "\\t(100,170,\\fscx100\\fscy100)\\fad(40,70)")
        elif o.motion_id == MotionId.FADE:
            tags.append("\\fad(140,140)")
        text = "{" + "".join(tags) + "}" + "\\N".join(p.lines)
        events.append(f"Dialogue: 0,{_ts(p.out_start_ms)},{_ts(p.out_end_ms)},"
                      f"{o.style_id},,0,0,0,,{text}")
    return "\n".join(head + events) + "\n"


# ── Avoid Map 어댑터 (SAM/Face/OCR 자리) ──────────────────────────────
class AvoidMapProvider:
    """실구현에서 SAM 3.1 / MediaPipe Face / PaddleOCR adapter로 교체."""
    def analyze(self, video_path: str, windows_ms: list[tuple[int, int]]) -> AvoidMap:
        raise NotImplementedError


class StaticAvoidMapProvider(AvoidMapProvider):
    """Mock: 미리 지정한 회피 영역 반환 (OCR 실패 시 기본 Safe Area 배치와 동일 효과)."""
    def __init__(self, avoid: AvoidMap | None = None):
        self._avoid = avoid or AvoidMap()

    def analyze(self, video_path, windows_ms):
        return self._avoid

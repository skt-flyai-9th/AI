from __future__ import annotations

import sys
from pathlib import Path


ENGINE_ROOT = Path(__file__).resolve().parents[1] / "reals-video-engine"
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from reals_edit_engine.contracts import (  # noqa: E402
    FontWeight,
    MotionId,
    Overlay,
    OverlayType,
    PlacementId,
)
from reals_edit_engine.subtitle_layout import PlacedOverlay, build_ass  # noqa: E402


class StubFontRegistry:
    @staticmethod
    def resolve_font(_font_asset_id: str, _weight: str) -> dict[str, object]:
        return {"ass_family": "Pretendard", "ass_bold": -1}


def _placed(style_id: str, *, motion_id: MotionId = MotionId.NONE) -> PlacedOverlay:
    overlay = Overlay(
        overlay_id=f"ov_{style_id.lower()}",
        produced_segment_id="ps_001",
        overlay_type=OverlayType.CAPTION,
        text_content="지금 확인하세요",
        style_id=style_id,
        start_ms=0,
        end_ms=1500,
        placement_id=PlacementId.AUTO_SAFE,
        motion_id=motion_id,
        font_weight=FontWeight.BOLD,
    )
    return PlacedOverlay(
        overlay=overlay,
        out_start_ms=0,
        out_end_ms=1500,
        x=123,
        y=700,
        font_px=64 if style_id == "CTA_BOX" else 92,
        lines=["지금 확인하세요"],
        text_width_px=380,
        text_height_px=76,
    )


def test_cta_box_uses_opaque_black_rounded_background():
    ass = build_ass([_placed("CTA_BOX")], StubFontRegistry())
    dialogues = [line for line in ass.splitlines() if line.startswith("Dialogue:")]

    assert len(dialogues) == 2
    background, text = dialogues
    assert background.startswith("Dialogue: 0,")
    assert "\\p1" in background
    assert "\\1c&H000000&\\1a&H00&" in background
    assert " b " in background
    assert "&H6E" not in ass
    assert text.startswith("Dialogue: 1,")


def test_subtitle_is_unrotated_and_forced_to_horizontal_center():
    ass = build_ass([_placed("HOOK")], StubFontRegistry(), canvas=(1080, 1920))
    dialogues = [line for line in ass.splitlines() if line.startswith("Dialogue:")]

    assert len(dialogues) == 1
    assert "\\pos(540,700)" in dialogues[0]
    assert "\\frz0" in dialogues[0]
    assert "\\frz356" not in ass


def test_background_and_text_share_the_same_motion():
    ass = build_ass([_placed("CTA_BOX", motion_id=MotionId.POP)], StubFontRegistry())
    dialogues = [line for line in ass.splitlines() if line.startswith("Dialogue:")]

    assert len(dialogues) == 2
    for dialogue in dialogues:
        assert "\\fscx74\\fscy74" in dialogue
        assert "\\t(0,100,\\fscx108\\fscy108)" in dialogue

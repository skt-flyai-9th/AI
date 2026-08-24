"""Registry JSON 생성기 — 폰트는 실제 파일 sha256으로 등록한다."""
import hashlib, json, pathlib

ROOT = pathlib.Path(__file__).parent
FONTS = ROOT / "assets" / "fonts"
REG = ROOT / "registry"

def sha(p): return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()

font_registry = {
    "font_registry_version": "1.0",
    "fonts": {
        "PRETENDARD": {
            "font_asset_id": "PRETENDARD",
            "family_name": "Pretendard",
            "files": {
                "REGULAR":  {"path": "assets/fonts/Pretendard-Regular.otf",  "sha256": sha(FONTS/"Pretendard-Regular.otf"),  "ass_family": "Pretendard",          "ass_bold": 0},
                "SEMIBOLD": {"path": "assets/fonts/Pretendard-SemiBold.otf", "sha256": sha(FONTS/"Pretendard-SemiBold.otf"), "ass_family": "Pretendard SemiBold", "ass_bold": 0},
                "BOLD":     {"path": "assets/fonts/Pretendard-Bold.otf",     "sha256": sha(FONTS/"Pretendard-Bold.otf"),     "ass_family": "Pretendard",          "ass_bold": -1},
            },
            "supported_scripts": ["KOREAN", "LATIN", "NUMERIC"],
            "commercial_license": "SIL-OFL-1.1",
            "license_file": "assets/fonts/OFL-LICENSE.txt",
            "fallback_allowed": False,
            "version": 1,
        }
    },
}

effect_registry = {
    "effect_registry_version": "1.0",
    "effects": {
        "NONE":       {"effect_id": "NONE", "support_level": "BASIC", "renderer_key": "noop", "allowed_params": {}, "version": 1},
        "PUNCH_ZOOM": {"effect_id": "PUNCH_ZOOM", "support_level": "BASIC", "renderer_key": "zoompan_v1",
                       "allowed_params": {"scale_end": {"min": 1.0, "max": 1.15}}, "version": 1},
        "SPEED":      {"effect_id": "SPEED", "support_level": "BASIC", "renderer_key": "setpts_v1",
                       "allowed_params": {"multiplier": {"min": 0.5, "max": 2.0}}, "version": 1},
        "COLOR_TONE": {"effect_id": "COLOR_TONE", "support_level": "BASIC", "renderer_key": "eq_v1",
                       "allowed_params": {"tone": {"enum": ["NATURAL", "WARM", "COOL"]}}, "version": 1},
    },
    "overlay_types": {
        "CAPTION": {"style_ids": ["CAPTION", "CAPTION_EMPHASIS", "CTA_BOX"], "motion_ids": ["NONE", "POP", "FADE"]},
        "TEXT_2D": {"style_ids": ["TEXT_2D"], "motion_ids": ["NONE", "POP", "FADE"]},
        "SFX":     {"intent_ids": ["TEXT_POP", "PRODUCT_REVEAL", "CTA_APPEAR", "FAST_TRANSITION", "RESULT_REVEAL"]},
    },
}

safe_area = {
    "profiles": {
        "INSTAGRAM_REELS_2026_V1": {
            "safe_area_profile_id": "INSTAGRAM_REELS_2026_V1",
            "canvas": {"width": 1080, "height": 1920},
            "blocked_regions": [
                {"id": "TOP_UI",         "x": 0,   "y": 0,    "w": 1080, "h": 260},
                {"id": "RIGHT_ACTIONS",  "x": 880, "y": 500,  "w": 200,  "h": 1100},
                {"id": "BOTTOM_CAPTION", "x": 0,   "y": 1500, "w": 1080, "h": 420},
            ],
            "version": 1,
        }
    }
}

render_profiles = {
    "profiles": {
        "INSTAGRAM_REELS_V1": {
            "render_profile_id": "INSTAGRAM_REELS_V1",
            "width": 1080, "height": 1920, "fps": 30,
            "video_codec": "libx264", "crf": 20, "preset": "medium", "pix_fmt": "yuv420p",
            "x264_profile": "high", "level": "4.0", "gop": 60,
            "audio_codec": "aac", "audio_bitrate": "128k", "audio_sample_rate": 48000,
            "max_duration_sec": 60, "max_file_size_bytes": 104857600,
            "movflags": "+faststart",
        },
        "INTERMEDIATE_VERTICAL_V1": {
            "render_profile_id": "INTERMEDIATE_VERTICAL_V1",
            "width": 1080, "height": 1920, "fps": 30,
            "video_codec": "libx264", "crf": 18, "preset": "medium", "pix_fmt": "yuv420p",
            "x264_profile": "high", "level": "4.0", "gop": 60,
            "audio_codec": "aac", "audio_bitrate": "192k", "audio_sample_rate": 48000,
            "max_duration_sec": 120, "max_file_size_bytes": 524288000,
            "movflags": "+faststart",
        },
    }
}

audio_mix = {
    "policies": {
        "SFX_ONLY_V1": {
            "audio_mix_policy_id": "SFX_ONLY_V1", "final_audio_policy": "SFX_ONLY",
            "sample_rate": 48000, "max_sfx_per_video": 3, "min_sfx_gap_ms": 300,
            "true_peak_limit_db": -1.0, "sfx_volume_db_range": {"min": -30, "max": -6},
            "original_audio_policy": "REMOVE", "bgm_policy": "NONE", "fallback_audio_mode": "SILENT",
        },
        "SILENT_V1": {
            "audio_mix_policy_id": "SILENT_V1", "final_audio_policy": "SILENT",
            "sample_rate": 48000, "max_sfx_per_video": 0, "min_sfx_gap_ms": 0,
            "true_peak_limit_db": -1.0, "sfx_volume_db_range": {"min": -30, "max": -6},
            "original_audio_policy": "REMOVE", "bgm_policy": "NONE", "fallback_audio_mode": "SILENT",
        },
    }
}

edit_policies = {
    "edit_policy_version": "1.0",
    "min_cut_duration_ms": 300,
    "max_caption_chars": 40,
    "max_captions_per_video": 8,
    "max_overlay_concurrent": 2,
    "caption_margin_px": 60,
    "min_font_px": 44,
    "max_caption_lines": 2,
    "cut_assembly": {
        "reorder_allowed": False,
        "major_segment_deletion_allowed": False,
        "edge_trim_allowed": True,
        "max_edge_trim_ms": 1500,
        "confidence_auto": 0.80,
        "confidence_limited": 0.55,
        "limited_max_trim_ms": 1000,
        "low_confidence_fallback": "KEEP_FULL_CUT",
    },
}

manifest = {"template_bundle_id": "tb_local_dev_001", "template_version": "4.1-runtime.dev", "files": {}}
REG.mkdir(exist_ok=True)
for name, data in [("font_registry", font_registry), ("effect_registry", effect_registry),
                   ("safe_area_profiles", safe_area), ("render_profiles", render_profiles),
                   ("audio_mix_policy", audio_mix), ("edit_policies", edit_policies)]:
    p = REG / f"{name}.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    manifest["files"][name] = "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()
(REG / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
print("registries written:", sorted(p.name for p in REG.iterdir()))

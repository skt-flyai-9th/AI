from __future__ import annotations


def trade_area_payload() -> dict:
    return {
        "name": "오피스 상권 분석 테스트",
        "description": "집계 신호만 사용하는 테스트용 상권 분석 템플릿입니다.",
        "industry_categories": ["all"],
        "area_types": ["office"],
        "analysis_dimensions": [
            {
                "key": "population_mix",
                "description": "시간대별 집계 인구 구성",
                "evidence_keys": ["population_by_hour", "age_distribution"],
            }
        ],
        "inference_rules": [
            {
                "rule_id": "peak_time",
                "description": "충분한 표본에서만 피크 시간을 요약",
                "when": {
                    "evidence_keys": ["visits_by_hour"],
                    "operator": "TOP_SHARE",
                    "threshold": 1.3,
                    "threshold_max": None,
                    "minimum_sample_size": 30,
                },
                "outputs": {
                    "characteristic_candidates": ["평일 점심 집중형"],
                    "include_top_age_ranges": 0,
                    "include_peak_time": True,
                    "caution": None,
                },
                "minimum_confidence": 0.65,
            }
        ],
        "recommendation_hints": ["집계 신호 우선"],
        "prompt_context": "집계 수치 안에서만 요약하고 불확실성을 표시합니다.",
        "policy": {
            "aggregate_only": True,
            "no_individual_attribute_assertions": True,
            "minimum_sample_size": 30,
            "conflicting_signals": "REPORT_UNCERTAINTY",
            "sensitive_attribute_inference": "FORBIDDEN",
        },
    }


def video_editing_db_payload() -> dict:
    return {
        "name": "메뉴 결과 선공개 테스트",
        "recommendation_title": "완성 메뉴를 먼저 보여주세요",
        "recommendation_concept": "결과와 핵심 디테일을 순서대로 보여줍니다.",
        "recommendation_metadata": {
            "supported_subject_types": ["MENU", "PRODUCT"],
            "supported_objectives": ["awareness", "visit"],
            "supported_filming_times": ["within_5m", "within_10m"],
            "supported_face_modes": ["allowed", "not_allowed"],
            "minimum_filming_time": "within_5m",
            "requires_face": False,
            "requires_tts": False,
            "requires_photo_input": False,
            "renderer_supported": True,
            "source_type": "VIDEO_ONLY",
            "difficulty": "하",
        },
        "shooting_guide": {
            "estimated_shooting_sec": 180,
            "difficulty": "하",
            "scenes": [
                {
                    "scene_order": 1,
                    "scene_role": "HOOK",
                    "scene_description": "완성 메뉴를 먼저 촬영합니다.",
                    "scene_dialogue": None,
                    "scene_subtitle": None,
                    "shot_type": "클로즈업",
                    "target_duration_sec": 2.5,
                }
            ],
            "tasks": [{"task_order": 1, "description": "세로 화면으로 촬영합니다."}],
        },
        "editing_rules": {
            "source_type": "VIDEO_ONLY",
            "render_profile_id": "INSTAGRAM_REELS_V1",
            "assembly_profile_id": "INTERMEDIATE_VERTICAL_V1",
            "safe_area_profile_id": "INSTAGRAM_REELS_2026_V1",
            "audio_policy": "SILENT_V1",
            "min_cut_duration_ms": 300,
            "max_duration_sec": 30,
            "allowed_effect_ids": ["PUNCH_ZOOM"],
            "allowed_transition_ids": ["CUT", "HARD_CUT"],
        },
        "trend_ids": [],
    }

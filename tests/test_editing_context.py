from app.agents.editing import llm as llm_module
from app.agents.editing.context_builder import build_editing_context
from app.agents.editing.llm import OpenAIEditingLLM
from app.agents.editing.types import EditingPlanDecision, VideoContext


def _video(video_id: str, order: int) -> VideoContext:
    return VideoContext(
        video_id=video_id,
        shooting_scene_order=order,
        duration_ms=3000,
        width=1080,
        height=1920,
        fps=30,
        keyframes=[],
    )


def _database() -> dict:
    return {
        "name": "카페 추천",
        "recommendation_title": "대표 메뉴 소개",
        "recommendation_concept": "메뉴를 먼저 보여주는 리뷰",
        "shooting_guide": {
            "scenes": [
                {
                    "scene_order": 1,
                    "scene_role": "HOOK",
                    "scene_description": "완성 메뉴를 먼저 보여준다.",
                    "shot_type": "CLOSE_UP",
                    "target_duration_sec": 2.0,
                },
                {
                    "scene_order": 2,
                    "scene_role": "PROCESS",
                    "scene_description": "메뉴를 만드는 과정을 보여준다.",
                    "shot_type": "MEDIUM",
                    "target_duration_sec": 3.0,
                },
            ],
            "tasks": [
                {
                    "display_order": 1,
                    "task_title": "완성 메뉴 촬영",
                    "scene_index": 0,
                    "guide": {"instructions": ["메뉴를 화면 중앙에 둡니다."]},
                },
                {
                    "display_order": 2,
                    "task_title": "제조 과정 촬영",
                    "scene_index": 1,
                    "guide": {"instructions": ["손의 움직임을 따라갑니다."]},
                },
            ],
        },
        "editing_rules": {"min_cut_duration_ms": 300},
        "reference_evidence": {"reference_segments": [{"id": "ref_1"}]},
    }


def test_multi_cut_context_joins_ordered_footage_to_guide_and_observations():
    result = build_editing_context(
        project={
            "project_id": "project_1",
            "store_id": "store_1",
            "promotion_subject": {"type": "MENU", "name": "라떼"},
            "promotion_objective": "sales",
            "face_exposure": "not_allowed",
            "shortform_context": {
                "project_state": {
                    "promotion_subject": {"type": "MENU", "name": "라떼"},
                    "promotion_objective": "sales",
                    "creative_preferences": ["밝고 빠르게"],
                    "secondary_information": ["매일 직접 만드는 크림"],
                    "facts_from_user": {"taste": "고소하고 부드러운 맛"},
                    "brief_confirmed": True,
                },
                "store_context": {"store": {"store_name": "테스트 카페"}},
                "recommendation": {"title": "대표 메뉴 소개"},
                "recent_user_statements": ["크림을 꼭 강조해줘"],
            },
        },
        selected_shortform={
            "editing_template_id": "gt_cafe",
            "editing_template_version": 2,
        },
        video_editing_db=_database(),
        video_contexts=[_video("take_2", 2), _video("take_1", 1)],
        prepared_analysis={
            "source_preparation": {
                "mode": "MULTI_CUT",
                "cuts": [
                    {
                        "video_id": "take_1",
                        "trim_in_ms": 100,
                        "trim_out_ms": 2100,
                        "mapped_reference_segment_id": "ref_1",
                        "decision_reason": "menu reveal",
                    },
                    {
                        "video_id": "take_2",
                        "trim_in_ms": 200,
                        "trim_out_ms": 2500,
                        "mapped_reference_segment_id": "ref_2",
                        "decision_reason": "process",
                    },
                ],
            },
            "produced_frame_context": {
                "mode": "MULTI_CUT",
                "observations": [
                    {
                        "video_id": "take_1",
                        "semantic_event": "MENU_REVEAL",
                        "subject": "라떼",
                        "action": "메뉴를 내려놓음",
                        "composition": "CLOSE_UP",
                        "camera_motion": "STATIC",
                        "quality_flags": ["LOW_LIGHT"],
                    }
                ],
            },
        },
    )

    assert result["context_version"] == "editing-context-v2"
    assert result["project_brief"]["verified_user_facts"] == {"taste": "고소하고 부드러운 맛"}
    assert result["project_brief"]["recent_user_statements"] == ["크림을 꼭 강조해줘"]
    assert result["project_brief"]["copy_directives"] == {}
    assert result["template_context"]["shoot_mode"] == "MULTI_CUT"
    assert result["template_context"]["reference_segment_count"] == 1
    assert [item["video_id"] for item in result["source_scenes"]] == ["take_1", "take_2"]
    first = result["source_scenes"][0]
    assert first["expected_scenes"][0]["scene_role"] == "HOOK"
    assert first["expected_tasks"][0]["task_title"] == "완성 메뉴 촬영"
    assert first["selected_source"]["mapped_reference_segment_id"] == "ref_1"
    assert first["observed_context"]["semantic_events"] == ["MENU_REVEAL"]
    assert first["observed_context"]["quality_flags"] == ["LOW_LIGHT"]


def test_one_take_context_exposes_the_full_expected_guide_flow():
    result = build_editing_context(
        project={},
        selected_shortform={},
        video_editing_db=_database(),
        video_contexts=[_video("take_1", 1)],
        prepared_analysis={
            "source_preparation": {
                "mode": "ONE_TAKE_PASSTHROUGH",
                "video_id": "take_1",
                "trim_in_ms": 0,
                "trim_out_ms": 3000,
            },
            "produced_frame_context": {"mode": "ONE_TAKE", "observations": []},
        },
    )

    source = result["source_scenes"][0]
    assert result["template_context"]["shoot_mode"] == "ONE_TAKE"
    assert [item["scene_role"] for item in source["expected_scenes"]] == [
        "HOOK",
        "PROCESS",
    ]
    assert source["selected_source"]["decision_reason"] == "ONE_TAKE_PASSTHROUGH"


def test_editing_request_subject_and_objective_override_stale_session_state():
    result = build_editing_context(
        project={
            "promotion_subject": {"type": "MENU", "name": "참치마요오니"},
            "promotion_objective": "sales",
            "shortform_context": {
                "project_state": {
                    "promotion_subject": {"type": "MENU", "name": "이전 메뉴"},
                    "promotion_objective": "trust",
                    "brief_confirmed": True,
                }
            },
        },
        selected_shortform={},
        video_editing_db=_database(),
        video_contexts=[_video("take_1", 1)],
        prepared_analysis={
            "source_preparation": {"mode": "ONE_TAKE_PASSTHROUGH"},
            "produced_frame_context": {"mode": "ONE_TAKE", "observations": []},
        },
    )

    assert result["project_brief"]["promotion_subject"]["name"] == "참치마요오니"
    assert result["project_brief"]["promotion_objective"] == "sales"


def test_recipe_planner_receives_the_preprocessed_editing_context(monkeypatch):
    planner = OpenAIEditingLLM()
    captured = {}
    prepared = {
        "source_preparation": {"mode": "ONE_TAKE_PASSTHROUGH"},
        "produced_frame_context": {"mode": "ONE_TAKE", "observations": []},
    }
    monkeypatch.setattr(planner, "_prepare_frame_analysis", lambda **_kwargs: prepared)
    monkeypatch.setattr(llm_module, "_renderer_capabilities", lambda: {})

    def capture(**kwargs):
        captured.update(kwargs["user_payload"])
        return EditingPlanDecision(
            outcome="SOURCE_GAP",
            recipe=None,
            publishing=None,
            missing_scene_roles=["RESULT"],
            available_options=["USE_REDUCED_STRUCTURE", "ADD_MORE_VIDEO"],
            rationale="test",
        )

    monkeypatch.setattr(planner, "_request_model", capture)
    planner.plan_recipe(
        domain_context="context",
        project={"project_id": "project_1"},
        selected_shortform={"editing_template_id": "gt_cafe"},
        video_editing_db=_database(),
        video_contexts=[_video("take_1", 1)],
        parent_recipe=None,
        revision_action=None,
    )

    assert captured["editing_context"]["context_version"] == "editing-context-v2"
    assert captured["editing_context"]["source_scenes"][0]["video_id"] == "take_1"

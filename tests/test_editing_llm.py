from app.agents.editing import llm as llm_module
from app.agents.editing.llm import OpenAIEditingLLM
from app.agents.editing.types import EditingPlanDecision


def _prepared_analysis():
    return {
        "source_preparation": {"mode": "MULTI_CUT", "cuts": []},
        "produced_frame_context": {"mode": "MULTI_CUT", "observations": []},
    }


def _source_gap_decision():
    return EditingPlanDecision(
        outcome="SOURCE_GAP",
        recipe=None,
        publishing=None,
        missing_scene_roles=["result"],
        available_options=["USE_REDUCED_STRUCTURE", "ADD_MORE_VIDEO"],
        rationale="test",
    )


def test_reduced_structure_revision_forces_recipe_policy(monkeypatch):
    captured = {}
    planner = OpenAIEditingLLM()
    monkeypatch.setattr(llm_module, "_renderer_capabilities", lambda: {})
    monkeypatch.setattr(planner, "_prepare_frame_analysis", lambda **_kwargs: _prepared_analysis())

    def capture(**kwargs):
        captured.update(kwargs["user_payload"])
        return _source_gap_decision()

    monkeypatch.setattr(planner, "_request_model", capture)

    planner.plan_recipe(
        domain_context="context",
        project={},
        selected_shortform={},
        video_editing_db={},
        video_contexts=[],
        parent_recipe=None,
        revision_action="USE_REDUCED_STRUCTURE",
    )

    assert captured["source_gap_policy"]["must_return_recipe"] is True
    assert "complete reduced-structure EditRecipe" in captured["task"]
    assert any("return RECIPE" in item for item in captured["requirements"])


def test_normal_editing_keeps_source_gap_detection_policy(monkeypatch):
    captured = {}
    planner = OpenAIEditingLLM()
    monkeypatch.setattr(llm_module, "_renderer_capabilities", lambda: {})
    monkeypatch.setattr(planner, "_prepare_frame_analysis", lambda **_kwargs: _prepared_analysis())

    def capture(**kwargs):
        captured.update(kwargs["user_payload"])
        return _source_gap_decision()

    monkeypatch.setattr(planner, "_request_model", capture)

    planner.plan_recipe(
        domain_context="context",
        project={},
        selected_shortform={},
        video_editing_db={},
        video_contexts=[],
        parent_recipe=None,
        revision_action=None,
    )

    assert captured["source_gap_policy"] == {"mode": "DETECT_REQUIRED_ROLE_GAPS"}
    assert not any("return RECIPE" in item for item in captured["requirements"])


def test_restored_analysis_checkpoint_skips_frame_reanalysis(monkeypatch):
    planner = OpenAIEditingLLM()
    shoot_mode = llm_module._resolve_shoot_mode({}, [])
    cache_key = llm_module._analysis_cache_key({}, [], shoot_mode)
    planner.restore_analysis_checkpoint(
        {"cache_key": cache_key, "prepared": _prepared_analysis()}
    )
    monkeypatch.setattr(llm_module, "_renderer_capabilities", lambda: {})
    monkeypatch.setattr(
        planner,
        "_prepare_frame_analysis",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("analysis reran")),
    )
    monkeypatch.setattr(planner, "_request_model", lambda **_kwargs: _source_gap_decision())

    result = planner.plan_recipe(
        domain_context="context",
        project={},
        selected_shortform={},
        video_editing_db={},
        video_contexts=[],
        parent_recipe=None,
        revision_action=None,
    )

    assert result.outcome == "SOURCE_GAP"

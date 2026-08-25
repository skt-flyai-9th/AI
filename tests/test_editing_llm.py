from app.agents.editing import llm as llm_module
from app.agents.editing.llm import OpenAIEditingLLM


def test_reduced_structure_revision_forces_recipe_policy(monkeypatch):
    captured = {}
    planner = OpenAIEditingLLM()
    monkeypatch.setattr(llm_module, "_renderer_capabilities", lambda: {})

    def capture(_context, payload, _video_contexts, _schema_name):
        captured.update(payload)
        return object()

    monkeypatch.setattr(planner, "_request", capture)

    planner.plan_recipe(
        domain_context="context",
        project={},
        selected_shortform={},
        template={},
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

    def capture(_context, payload, _video_contexts, _schema_name):
        captured.update(payload)
        return object()

    monkeypatch.setattr(planner, "_request", capture)

    planner.plan_recipe(
        domain_context="context",
        project={},
        selected_shortform={},
        template={},
        video_contexts=[],
        parent_recipe=None,
        revision_action=None,
    )

    assert captured["source_gap_policy"] == {"mode": "DETECT_REQUIRED_ROLE_GAPS"}
    assert not any("return RECIPE" in item for item in captured["requirements"])

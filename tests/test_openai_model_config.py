from app.core.config import Settings


def test_all_gpt_components_default_to_gpt_4_1_mini(monkeypatch):
    for name in (
        "SHORTFORM_OPENAI_MODEL",
        "DATABASE_OPENAI_MODEL",
        "EDITING_OPENAI_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)

    assert settings.shortform_openai_model == "gpt-5.4-mini"
    assert settings.database_openai_model == "gpt-5.4-mini"
    assert settings.editing_openai_model == "gpt-5.4-mini"

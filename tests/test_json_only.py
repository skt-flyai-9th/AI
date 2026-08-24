from pathlib import Path


def test_repository_has_no_html_ranking_generator():
    pipeline = Path("app/ranker_core/pipeline.py").read_text(encoding="utf-8")
    assert "ranking_latest" not in pipeline
    assert "trendcluster.html" not in pipeline
    assert "_write_public_html" not in pipeline

import yaml


def test_dual_video_weights_are_finalized():
    config = yaml.safe_load(open("config/pipeline.yaml", encoding="utf-8"))
    rep = config["representative_youtube"]["representative_weights"]
    guide = config["representative_youtube"]["guide_weights"]
    assert rep == {
        "relevance": 0.25,
        "participation": 0.10,
        "popularity": 0.45,
        "recency": 0.05,
        "engagement": 0.05,
        "kr_affinity": 0.10,
    }
    assert guide == {
        "relevance": 0.20,
        "guideability": 0.45,
        "participation": 0.10,
        "popularity": 0.05,
        "recency": 0.05,
        "engagement": 0.05,
        "kr_affinity": 0.10,
    }

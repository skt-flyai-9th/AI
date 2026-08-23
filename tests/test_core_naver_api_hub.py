from __future__ import annotations

import pandas as pd

from app.ranker_core.connectors.naver import (
    NAVER_API_HUB_CLIENT_ID_HEADER,
    NAVER_API_HUB_CLIENT_SECRET_HEADER,
    NaverBlogConnector,
    NaverDatalabConnector,
    NaverNewsConnector,
    _naver_api_hub_credentials,
    _naver_api_hub_headers,
)


def test_naver_api_hub_endpoints() -> None:
    assert (
        NaverDatalabConnector.url
        == "https://naverapihub.apigw.ntruss.com/search-trend/v1/search"
    )
    assert (
        NaverBlogConnector.url
        == "https://naverapihub.apigw.ntruss.com/search/v1/blog"
    )
    assert (
        NaverNewsConnector.url
        == "https://naverapihub.apigw.ntruss.com/search/v1/news"
    )


def test_naver_api_hub_credentials_and_headers(monkeypatch) -> None:
    monkeypatch.setenv("NAVER_API_HUB_CLIENT_ID", "hub-client-id")
    monkeypatch.setenv("NAVER_API_HUB_CLIENT_SECRET", "hub-client-secret")

    credentials = _naver_api_hub_credentials({})
    assert credentials == ("hub-client-id", "hub-client-secret")

    get_headers = _naver_api_hub_headers(*credentials)
    assert get_headers == {
        NAVER_API_HUB_CLIENT_ID_HEADER: "hub-client-id",
        NAVER_API_HUB_CLIENT_SECRET_HEADER: "hub-client-secret",
    }

    post_headers = _naver_api_hub_headers(*credentials, json_body=True)
    assert post_headers["Content-Type"] == "application/json"
    assert "X-Naver-Client-Id" not in post_headers
    assert "X-Naver-Client-Secret" not in post_headers


def test_search_trend_collect_passes_api_hub_headers(monkeypatch) -> None:
    monkeypatch.setenv("NAVER_API_HUB_CLIENT_ID", "id")
    monkeypatch.setenv("NAVER_API_HUB_CLIENT_SECRET", "secret")
    captured: dict[str, str] = {}

    def fake_collect(self, candidates, now, headers):
        captured.update(headers)
        return pd.DataFrame(
            {
                "challenge_id": candidates["challenge_id"],
                "naver_search_evidence": [1.0] * len(candidates),
            }
        )

    monkeypatch.setattr(NaverDatalabConnector, "_collect", fake_collect)
    connector = NaverDatalabConnector({}, "Asia/Seoul")
    result = connector.collect(
        pd.DataFrame({"challenge_id": ["test"]}),
        pd.Timestamp("2026-08-19T00:00:00Z"),
    )

    assert result.status["success"] is True
    assert captured == {
        "X-NCP-APIGW-API-KEY-ID": "id",
        "X-NCP-APIGW-API-KEY": "secret",
        "Content-Type": "application/json",
    }


def test_blog_and_news_collect_use_api_hub_requests(monkeypatch) -> None:
    monkeypatch.setenv("NAVER_API_HUB_CLIENT_ID", "id")
    monkeypatch.setenv("NAVER_API_HUB_CLIENT_SECRET", "secret")
    calls: list[dict[str, object]] = []

    def fake_request_json(session, method, url, **kwargs):
        calls.append(
            {
                "method": method,
                "url": url,
                "headers": kwargs.get("headers"),
                "params": kwargs.get("params"),
            }
        )
        return {"items": []}

    monkeypatch.setattr(
        "app.ranker_core.connectors.naver.request_json", fake_request_json
    )
    candidates = pd.DataFrame(
        {
            "challenge_id": ["test"],
            "name": ["테스트 챌린지"],
            "alias_list": [["테스트"]],
        }
    )
    now = pd.Timestamp("2026-08-19T00:00:00Z")

    blog_result = NaverBlogConnector(
        {
            "pages_per_challenge": 1,
            "display": 1,
            "max_aliases_per_challenge": 1,
        }
    ).collect(candidates, now)
    news_result = NaverNewsConnector(
        {"pages_per_challenge": 1, "display": 1}
    ).collect(candidates, now)

    assert blog_result.status["success"] is True
    assert news_result.status["success"] is True
    assert calls == [
        {
            "method": "GET",
            "url": "https://naverapihub.apigw.ntruss.com/search/v1/blog",
            "headers": {
                "X-NCP-APIGW-API-KEY-ID": "id",
                "X-NCP-APIGW-API-KEY": "secret",
            },
            "params": {
                "query": "테스트 챌린지",
                "display": 1,
                "start": 1,
                "sort": "date",
                "format": "json",
            },
        },
        {
            "method": "GET",
            "url": "https://naverapihub.apigw.ntruss.com/search/v1/news",
            "headers": {
                "X-NCP-APIGW-API-KEY-ID": "id",
                "X-NCP-APIGW-API-KEY": "secret",
            },
            "params": {
                "query": "테스트 챌린지",
                "display": 1,
                "start": 1,
                "sort": "date",
                "format": "json",
            },
        },
    ]


def test_search_trend_limits_keywords_to_five(monkeypatch) -> None:
    monkeypatch.setenv("NAVER_API_HUB_CLIENT_ID", "id")
    monkeypatch.setenv("NAVER_API_HUB_CLIENT_SECRET", "secret")
    captured: dict[str, object] = {}

    def fake_request_json(session, method, url, **kwargs):
        captured.update(kwargs.get("json") or {})
        return {"results": []}

    monkeypatch.setattr(
        "app.ranker_core.connectors.naver.request_json", fake_request_json
    )
    candidates = pd.DataFrame(
        {
            "challenge_id": ["test"],
            "name": ["테스트 챌린지"],
            "alias_list": [[f"별칭{i}" for i in range(10)]],
        }
    )
    connector = NaverDatalabConnector({"lookback_days": 14}, "Asia/Seoul")
    result = connector.collect(candidates, pd.Timestamp("2026-08-19T00:00:00Z"))

    assert result.status["success"] is True
    groups = captured["keywordGroups"]
    assert len(groups[0]["keywords"]) == 5

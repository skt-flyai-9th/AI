from __future__ import annotations

from app.schemas.trade_area_insight import StoreInfo
from app.services.trade_area_insight import build_trade_area_insight, load_trade_area_knowledge


def _sharosu_coordinates() -> tuple[float, float]:
    """샤로수길·서울대입구(REG-SHAROSU)에 매핑된 공식 상권의 실제 대표 좌표."""
    knowledge = load_trade_area_knowledge()
    for area in knowledge.areas:
        if (
            knowledge.area_to_region_id.get(area.trdar_cd) == "REG-SHAROSU"
            and area.latitude is not None
            and area.longitude is not None
        ):
            return area.latitude, area.longitude
    raise AssertionError("REG-SHAROSU에 매핑된 좌표가 있는 공식 상권을 찾지 못함")


def _assert_sums_to_100(distribution) -> None:
    values = list(distribution.model_dump(by_alias=True).values())
    assert sum(values) == 100
    assert all(v >= 0 for v in values)


def test_coordinate_match_resolves_known_district():
    lat, lon = _sharosu_coordinates()
    store = StoreInfo(
        name="테스트 분식",
        category="분식",
        sub_category="떡볶이/김밥",
        address="서울 관악구 어딘가 1",
        latitude=lat,
        longitude=lon,
    )
    result = build_trade_area_insight(store)
    assert result.district_name == "샤로수길·서울대입구"
    assert result.summary
    _assert_sums_to_100(result.age_distribution)
    _assert_sums_to_100(result.gender_distribution)


def test_no_match_falls_back_to_null_district_but_still_fills_other_fields():
    store = StoreInfo(
        name="어딘지모를가게",
        category="기타",
        sub_category=None,
        address="완전히 무관한 주소 999",
        latitude=None,
        longitude=None,
    )
    result = build_trade_area_insight(store)
    assert result.district_name is None
    assert result.summary
    _assert_sums_to_100(result.age_distribution)
    _assert_sums_to_100(result.gender_distribution)


def test_far_away_coordinates_do_not_force_a_match():
    # 서울 바깥(부산 근처) 좌표 - 가장 가까운 공식 상권도 임계거리를 훌쩍 넘어야 한다.
    store = StoreInfo(
        name="부산가게",
        category="카페",
        sub_category=None,
        address="부산 어딘가 1",
        latitude=35.1796,
        longitude=129.0756,
    )
    result = build_trade_area_insight(store)
    assert result.district_name is None


def test_endpoint_requires_internal_api_key(client):
    lat, lon = _sharosu_coordinates()
    response = client.post(
        "/api/v1/trade-area-insights",
        json={
            "store": {
                "name": "테스트 분식",
                "category": "분식",
                "sub_category": "떡볶이/김밥",
                "address": "서울 관악구 어딘가 1",
                "latitude": lat,
                "longitude": lon,
            }
        },
    )
    assert response.status_code == 401


def test_endpoint_returns_expected_response_shape(client, auth_headers):
    lat, lon = _sharosu_coordinates()
    response = client.post(
        "/api/v1/trade-area-insights",
        headers=auth_headers,
        json={
            "store": {
                "name": "테스트 분식",
                "category": "분식",
                "sub_category": "떡볶이/김밥",
                "address": "서울 관악구 어딘가 1",
                "latitude": lat,
                "longitude": lon,
            }
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["district_name"] == "샤로수길·서울대입구"
    assert set(body["age_distribution"].keys()) == {"10s", "20s", "30s", "40s", "50s_plus"}
    assert sum(body["age_distribution"].values()) == 100
    assert set(body["gender_distribution"].keys()) == {"male", "female"}
    assert sum(body["gender_distribution"].values()) == 100

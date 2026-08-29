from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.security import require_internal_api_key
from app.schemas.trade_area_insight import TradeAreaInsightRequest, TradeAreaInsightResponse
from app.services.trade_area_insight import build_trade_area_insight

router = APIRouter(
    prefix="/trade-area-insights",
    tags=["trade-area-insights"],
    dependencies=[Depends(require_internal_api_key)],
)


@router.post("", response_model=TradeAreaInsightResponse)
def create_trade_area_insight(payload: TradeAreaInsightRequest) -> TradeAreaInsightResponse:
    """마이페이지 "내 가게 상권 분석" 화면용 인사이트를 만든다.

    가게 등록 직후 backend가 한 번 호출해 `store_insights`에 저장하는 용도다
    (실시간 재계산 없음, MVP 기준). 좌표가 있으면 상권분석DB의 공식 상권 대표
    좌표까지의 직선거리로 가장 가까운 곳을 찾고(폴리곤 경계가 아직 없어 근사치),
    없으면 주소 문자열로 행정동을 대조한다. 뚜렷한 상권을 특정하지 못하면
    `district_name`은 null이고, 나머지 필드는 서울 전체 평균 등으로 최대한 채운다.
    """
    return build_trade_area_insight(payload.store)

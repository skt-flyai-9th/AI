from __future__ import annotations

import os
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

from app.ranker_core.connectors.apify_instagram import collect_popular_reels_resilient
from app.ranker_core.gemini_json import resolve_gemini_model

load_dotenv()


def sep():
    print("\n" + "=" * 72)


def preview(r: requests.Response) -> str:
    return r.text.replace("\n", " ")[:900]


def test_apify():
    sep(); print("[1] APIFY / INSTAGRAM")
    token = os.getenv("APIFY_API_TOKEN", "").strip()
    if not token:
        print("❌ APIFY_API_TOKEN 없음"); return
    try:
        items, report = collect_popular_reels_resilient(
            token=token,
            seeds=["챌린지", "댄스", "KPOP"],
            search_limit=2,
            timeout_seconds=120,
            max_seed_runs=3,
        )
        if items:
            print(f"✅ Apify Actor 호출 정상 / rows={len(items)} / successful_terms={report.get('successful_terms')}")
            print("reel:", items[0].get("url") or items[0].get("shortCode"))
        else:
            print("⚠️ Apify 토큰 호출은 시도했지만 popular reels 결과가 0개입니다.")
            for failure in (report.get("failed") or [])[:5]:
                print("  -", failure.get("term"), ":", failure.get("reason"))
    except Exception as e:
        print("❌ Apify 실패:", e)


def test_gemini():
    sep(); print("[2] GEMINI")
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        print("❌ GEMINI_API_KEY 없음"); return
    try:
        model = resolve_gemini_model(key, "auto")
        print("✅ Models API 정상 / 자동 선택 모델:", model)
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        r = requests.post(
            url,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": "OK 한 단어로만 답하세요."}]}]},
            timeout=45,
        )
        print("generateContent STATUS:", r.status_code)
        print("✅ Gemini 생성 정상" if r.status_code == 200 else "❌ Gemini 생성 실패")
        if r.status_code != 200: print(preview(r))
    except Exception as e:
        print("❌ Gemini 실패:", e)


def test_youtube():
    sep(); print("[3] YOUTUBE DATA API")
    key = os.getenv("YOUTUBE_API_KEY", "").strip()
    if not key:
        print("❌ YOUTUBE_API_KEY 없음"); return
    # channels.list only costs normal quota and does not consume the dedicated
    # search.list daily bucket reserved for the Top-100 ranking run.
    r = requests.get(
        "https://www.googleapis.com/youtube/v3/channels",
        params={"part":"snippet","id":"UC_x5XG1OV2P6uZZ5FSM9Ttw","maxResults":1,"key":key},
        timeout=30,
    )
    print("STATUS:", r.status_code)
    if r.status_code == 200:
        print("✅ YouTube API 키/서비스 정상 (search.list 호출 0회)")
    else:
        print("❌ YouTube 실패", preview(r))


def test_naver():
    sep(); print("[4] NAVER API HUB")
    cid = os.getenv("NAVER_API_HUB_CLIENT_ID", "").strip()
    secret = os.getenv("NAVER_API_HUB_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        print("⚠️ NAVER 키 없음 (선택/강력 권장)"); return
    headers = {"X-NCP-APIGW-API-KEY-ID":cid,"X-NCP-APIGW-API-KEY":secret}
    base = "https://naverapihub.apigw.ntruss.com"
    for label, path in [("News","/search/v1/news"),("Blog","/search/v1/blog")]:
        r = requests.get(base+path, headers=headers, params={"query":"챌린지","display":1,"sort":"date"}, timeout=30)
        print(f"{label} STATUS:", r.status_code, "✅" if r.status_code == 200 else "❌")
        if r.status_code != 200: print(preview(r))
    end = date.today() - timedelta(days=1); start = end - timedelta(days=6)
    r = requests.post(
        base+"/search-trend/v1/search",
        headers={**headers,"Content-Type":"application/json"},
        json={"startDate":start.isoformat(),"endDate":end.isoformat(),"timeUnit":"date","keywordGroups":[{"groupName":"챌린지","keywords":["챌린지","댄스 챌린지","유행 챌린지"]}]},
        timeout=30,
    )
    print("Search Trend STATUS:", r.status_code, "✅" if r.status_code == 200 else "❌")
    if r.status_code != 200: print(preview(r))


if __name__ == "__main__":
    test_apify(); test_gemini(); test_youtube(); test_naver(); sep(); print("API 점검 완료")

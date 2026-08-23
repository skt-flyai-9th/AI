# 국내 유행 챌린지 Top 100 API

Instagram Reels 스타일의 국내 유행 챌린지를 자동 발견하고, 국내 확산도를 계산해 Top 100을 제공하는 FastAPI 백엔드입니다. 각 챌린지에는 목적이 다른 두 개의 YouTube 링크를 제공합니다.

- `representative_youtube_url`: 앱 카드/상세 상단에 노출할 유명하고 조회수가 높은 대표 참여 영상
- `guide_youtube_url`: 사용자가 실제로 따라 할 수 있도록 동작이 잘 보이는 안무·튜토리얼·시범 영상

HTML 랭킹 파일은 생성하지 않습니다. PostgreSQL이 원본 데이터이며 FastAPI JSON 응답과 `exports/ranking_latest.json`만 제공합니다.

## 전체 구조

```text
Apify Instagram popular reels
  → hashtag/audio 자동 확장
  → Gemini 후보 군집화·오탐 제거
  → Instagram 확산 재검증
  → NAVER API HUB 국내성 검증
  → provisional Top 100
  → YouTube 후보 수집
  → 대표영상/가이드영상 별도 선정
  → PostgreSQL 저장
  → FastAPI JSON API
```

역할은 명확히 분리합니다.

- **Instagram/Apify**: 챌린지 후보 발견 및 실제 참여·확산 검증
- **Gemini**: 서로 다른 표현·음원·해시태그·영상 패턴 군집화와 false positive 제거
- **NAVER API HUB**: 검색 트렌드·블로그·뉴스를 통한 국내 유행 여부 검증
- **YouTube**: 교차 플랫폼 확산 검증과 앱용 대표/가이드 영상 공급

TikTok API는 사용하지 않습니다.

## 대표영상 선정 기준

앱 화면용 대표영상은 유명도와 조회 성과를 가장 크게 평가합니다.

| 기준 | 비중 |
|---|---:|
| 조회수/유명도 | 45% |
| 챌린지 관련성 | 25% |
| 실제 참여/시범 여부 | 10% |
| 국내 관련성 | 10% |
| 최신성 | 5% |
| 참여율 | 5% |

## 가이드영상 선정 기준

따라하기용 가이드영상은 동작이 명확한지 여부를 가장 크게 평가합니다.

| 기준 | 비중 |
|---|---:|
| 안무/튜토리얼/동작 명료도 | 45% |
| 챌린지 관련성 | 20% |
| 실제 참여/시범 여부 | 10% |
| 국내 관련성 | 10% |
| 조회수 | 5% |
| 최신성 | 5% |
| 참여율 | 5% |

`tutorial`, `튜토리얼`, `거울모드`, `mirrored`, `slow`, `천천히`, `step by step`, `안무영상`, `dance practice`, `choreography`, `연습영상`, `전체 안무`, `시범`에 가점을 줍니다. 전용 가이드가 없으면 동작이 잘 보이는 실제 참여 영상으로 fallback합니다.

YouTube 공개 Data API에는 일반 영상의 `shareCount`가 없으므로 참여율은 `likeCount`, `commentCount`, `viewCount` 중심으로 계산합니다.

## 기존 단일 대표영상 원칙

초기 단일 영상 선정 기준은 다음과 같습니다.

- 챌린지 관련성 40%
- 실제 참여/따라하기 여부 20%
- 조회 성과 15%
- 최신성 10%
- 참여율 5%
- 국내 관련성 10%

가중합 전에 관련성 기준 미달, `COMMENTARY`, `UNRELATED` 후보를 제외합니다. 목적은 단순히 가장 인기 있는 영상을 고르는 것이 아니라, 사용자가 클릭했을 때 해당 국내 유행 챌린지를 빠르게 이해할 수 있는 실제 참여 영상을 고르는 것입니다. 이 철학은 현재 가이드영상 선정에 계승되었습니다.

## Apify 도입 원칙과 한계

YouTube 중심 Discovery는 게임 미션, 장기 목표, 유튜버 자체 기획, 해설 영상 등 YouTube 스타일 콘텐츠에 편향됩니다. 따라서 Discovery 중심을 Instagram으로 이동합니다.

1. Instagram Search Scraper의 popular reels를 내부 seed keyword로 탐색합니다.
2. 발견한 hashtag/audio를 자동 확장해 재탐색합니다.
3. 필요하면 Hashtag Scraper와 Reel Scraper로 고유 크리에이터 수, 음원 반복, 최근 게시 증가, 실제 행동 유사성을 검증합니다.

Apify를 추가하면 릴스형 춤, 포즈, 손동작, 전환, 변신, 특정 음원, 이름 없는 초기 챌린지 발견이 개선됩니다. 단, Apify는 Meta 공식 Trend API가 아니며 keyword seed가 필요하고 필드가 불안정할 수 있습니다. 한국 audience geo를 직접 보장하지 않으므로 한국어 caption/ASR/hashtag, creator/location, NAVER/YouTube 신호를 함께 사용합니다.

Meta 자동수집 약관과 상업적 사용 리스크는 별도 검토가 필요합니다. 수집부는 `InstagramSourceAdapter`로 추상화하여 향후 허가된 공급사나 공식 접근 방식으로 교체할 수 있게 설계하는 것이 원칙입니다. 초기에는 Search Scraper popular reels만 연결해 기존 결과와 A/B 비교한 뒤 Hashtag/Reel 분석을 확장합니다.

## 빠른 실행: Docker Compose

```bash
cp .env.example .env
# .env에 API 키와 ADMIN_API_TOKEN 입력
docker compose up --build -d
```

서비스:

- API: `http://localhost:8000`
- Swagger: `http://localhost:8000/docs`
- PostgreSQL
- Redis
- Celery Worker
- Celery Beat

## 필수 환경변수

```dotenv
APIFY_API_TOKEN=
GEMINI_API_KEY=
YOUTUBE_API_KEY=
NAVER_API_HUB_CLIENT_ID=
NAVER_API_HUB_CLIENT_SECRET=
ADMIN_API_TOKEN=change-this
```

## API

### Top 100

```http
GET /api/v1/challenges?limit=100
```

```json
{
  "generated_at": "2026-08-23T15:00:00Z",
  "count": 100,
  "results": [
    {
      "id": "bad-challenge",
      "rank": 1,
      "name": "BAD 챌린지",
      "representative_youtube_url": "https://www.youtube.com/watch?v=AAA",
      "guide_youtube_url": "https://www.youtube.com/watch?v=BBB"
    }
  ]
}
```

### 배치 실행

```http
POST /api/v1/ranking-runs
X-Admin-Token: <ADMIN_API_TOKEN>
```

### 실행 상태

```http
GET /api/v1/ranking-runs/{run_id}
```

### 수동 수정

```http
PATCH /api/v1/challenges/{challenge_id}
X-Admin-Token: <ADMIN_API_TOKEN>
Content-Type: application/json

{
  "rank": 3,
  "name": "BAD 댄스 챌린지",
  "representative_youtube_url": "https://www.youtube.com/watch?v=NEW1",
  "guide_youtube_url": "https://www.youtube.com/watch?v=NEW2"
}
```

수동 수정은 override 컬럼으로 저장되므로 다음 자동 배치가 덮어쓰지 않습니다. 필드에 `null`을 보내면 해당 override를 해제하고 자동 분석값으로 돌아갑니다.

## JSON 직접 수정/import

`data/ranking_overrides.example.json` 형식으로 파일을 편집한 뒤 실행합니다.

```bash
python -m app.cli import-overrides data/ranking_overrides.json
```

DB가 원본이며 JSON 파일은 import/export 인터페이스입니다.

## 로컬 개발

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
```

동기 배치 실행:

```bash
python -m app.cli run-ranking
```

API 연결 점검:

```bash
python scripts/check_apis.py
```

테스트:

```bash
ruff check .
pytest
```

## 장애 처리

- Apify 일부 seed 실패: 해당 seed만 건너뜁니다.
- Gemini 429/실패: 제한 재시도 후 현재까지 수집된 데이터로 degraded 실행합니다.
- YouTube 429: 추가 영상 검색을 중단하되 랭킹 자체는 저장합니다.
- NAVER 실패: 해당 지표만 비우고 계속 실행합니다.
- 장시간 작업은 FastAPI 요청 안에서 수행하지 않고 Celery Worker에서 수행합니다.

## 주요 엔드포인트

| Method | Endpoint | 설명 |
|---|---|---|
| GET | `/api/v1/health/live` | 프로세스 상태 |
| GET | `/api/v1/health/ready` | DB/API Key 준비 상태 |
| GET | `/api/v1/challenges?limit=100` | 현재 Top 100 |
| GET | `/api/v1/challenges/{id}` | 챌린지 상세 |
| PATCH | `/api/v1/challenges/{id}` | 랭킹/이름/영상 수동 override |
| POST | `/api/v1/ranking-runs` | 분석 배치 시작 |
| GET | `/api/v1/ranking-runs/{id}` | 배치 상태 |
| POST | `/api/v1/overrides/import` | JSON override 일괄 반영 |

# FLY AI Service

메인 백엔드와 **별도로 배포되는 독립 AI 서버**입니다. 모바일·웹 클라이언트가 이 서버를 직접 호출하는 구조가 아니라, 메인 백엔드가 내부 API로 AI 작업을 요청하고 JSON 결과를 받아 서비스 응답에 활용하는 구조를 전제로 합니다.

```text
Mobile / Web
    ↓
Main Backend
    ↓  X-Internal-API-Key
FLY AI Service (this repository)
    ↓
Apify / Gemini / YouTube / NAVER API HUB
```

이 저장소는 여러 AI Agent를 독립 경계로 제공하는 서버입니다. 현재 `challenge-ranking`, `shortform`, `editing` 세 Agent를 사용할 수 있습니다.

Agent와 별도로, 검증된 GPU 기반 숏폼 렌더링 파이프라인은 [`reals-video-engine/`](reals-video-engine/)에 독립 모듈로 포함되어 있습니다.

## 현재 구현 상태

| Agent ID | 상태 | 역할 |
|---|---|---|
| `challenge-ranking` | `AVAILABLE` | 국내 유행 챌린지 Top 100 분석, 대표영상·가이드영상 선정 |
| `shortform` | `AVAILABLE` | 대화로 프로젝트 brief를 확정하고 호환 편집 템플릿 1개 추천 |
| `editing` | `AVAILABLE` | 촬영 영상 컨텍스트 분석, EditRecipe 검증·수정, Renderer 실행 |

등록된 Agent는 다음 API에서 확인할 수 있습니다.

```http
GET /api/v1/agents
X-Internal-API-Key: <INTERNAL_API_KEY>
```

새 Agent를 추가할 때의 구조와 규칙은 [`docs/ADDING_AN_AGENT.md`](docs/ADDING_AN_AGENT.md)를 참고합니다.

## 숏폼 영상 편집 엔진

`reals-video-engine/`는 가이드 분석 결과와 촬영 영상을 받아 세로형 숏폼 MP4를 만드는 **독립 실행형 편집 엔진**입니다. `editing` Agent는 REALS registry 기반 preflight를 통과한 EditRecipe를 `reals-render-job-1.0` 계약으로 변환해 Renderer 서비스 경계로 전달하며, 엔진 내부에는 LLM을 두지 않습니다. 같은 registry manifest의 SHA-256을 AI와 Renderer 양쪽에서 검증해 지원 효과와 정책의 드리프트를 차단합니다.

```text
Guide Analysis / Orchestrator
  ↓ EditRecipe + 촬영 클립
REALS Video Edit Engine
  ├─ CUT_ASSEMBLY: 모션·품질 분석 → 순서 보존 트림·결합
  └─ FINAL_RENDER: Avoid Map → 자막·SFX → FFmpeg → Post-render QC
  ↓
MP4 + Cut/Render Manifest + QC 결과
```

엔진의 책임:

1. 입력 영상 정규화와 테스트용 컷 자동 준비
2. 촬영 순서를 보존한 컷 분석·트림·결합
3. SAM 3.1, MediaPipe, YOLO, PP-OCR 기반 보호 영역 분석
4. 자막·SFX 배치와 1080x1920 H.264/AAC 렌더
5. 코덱·해상도·길이·블랙 프레임·오디오 Post-render QC

### 현재 검증 상태

| 항목 | 결과 |
|---|---|
| GPU 환경 | RTX 4090 Laptop GPU 16GB |
| SAM 3.1 | 실영상 `person` 분할 및 엔진 통합 통과 |
| VRAM | 추론 최대 약 5.1GB, 종료 후 정상 해제 |
| 엔진 E2E | SAM 폴백 없이 최종 렌더 성공 |
| 출력 QC | 11개 항목 전체 통과 |

빠른 실행:

```bash
cd reals-video-engine
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python tools/fetch_models.py
python demo/run_gpu_stack.py --video sample.mp4
```

GPU 설치, Hugging Face 승인·로그인, 고정 의존성 및 운영 정책은 [엔진 README](reals-video-engine/README.md)를 참고합니다. 모델 가중치, 사용자 영상, 실행 결과, 토큰은 저장소에 커밋하지 않습니다.

## 이 서버가 담당하는 범위

AI 서버의 책임:

1. 메인 백엔드의 랭킹 분석 요청 수신
2. Instagram/Apify 기반 후보 자동 발견
3. Gemini 기반 챌린지 군집화와 false positive 제거
4. NAVER API HUB 기반 국내 유행 검증
5. YouTube 대표영상·가이드영상 선정
6. 작업 상태와 결과를 PostgreSQL에 저장
7. 메인 백엔드에 JSON API로 결과 제공

AI 서버가 담당하지 않는 범위:

- 사용자 로그인·회원 관리
- 앱용 비즈니스 API
- 매장·프로젝트·촬영 데이터
- 프론트엔드 화면
- 메인 백엔드 DB의 도메인 모델
- 앱 클라이언트와의 직접 통신

AI 서버의 PostgreSQL은 분석 작업, 관측 데이터, 랭킹 스냅샷, 수동 override를 관리하기 위한 내부 저장소입니다. 메인 백엔드는 필요하면 최종 결과만 자체 DB에 복사하거나 캐시합니다.

## 챌린지 랭킹 Agent

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

플랫폼 역할은 다음과 같이 분리합니다.

- **Instagram/Apify**: 챌린지 후보 발견 및 실제 참여·확산 검증
- **Gemini**: 서로 다른 표현·음원·해시태그·영상 패턴 군집화, false positive 제거
- **NAVER API HUB**: 검색 트렌드·블로그·뉴스를 통한 국내 유행 여부 검증
- **YouTube**: 교차 플랫폼 확산 검증과 대표·가이드 영상 제공

TikTok API는 사용하지 않습니다.

## 메인 백엔드 연동 요약

모든 내부 API 호출에는 다음 헤더를 사용합니다.

```http
X-Internal-API-Key: <INTERNAL_API_KEY>
```

초기 버전의 `X-Admin-Token`도 호환을 위해 당분간 지원하지만, 신규 연동에서는 사용하지 않습니다.

### 1. 랭킹 분석 시작

```http
POST /api/v1/ranking-runs
X-Internal-API-Key: <INTERNAL_API_KEY>
```

응답은 `202 Accepted`입니다.

```json
{
  "run_id": "6cd61210-7a14-4dd0-a85c-364dd24a61a5",
  "status": "QUEUED",
  "task_id": "f1018f72-c4dc-4e48-89d0-f9678a935845"
}
```

한 번의 POST는 한 번의 분석 작업을 생성합니다. 현재 webhook/callback은 구현되어 있지 않으므로 메인 백엔드가 상태 API를 polling합니다.

### 2. 작업 상태 확인

```http
GET /api/v1/ranking-runs/{run_id}
X-Internal-API-Key: <INTERNAL_API_KEY>
```

```json
{
  "id": "6cd61210-7a14-4dd0-a85c-364dd24a61a5",
  "status": "RUNNING",
  "stage": "COLLECTING_AND_ANALYZING",
  "progress": 40,
  "celery_task_id": "f1018f72-c4dc-4e48-89d0-f9678a935845",
  "error_message": null,
  "warnings": [],
  "source_status": {},
  "created_at": "2026-08-23T08:00:00Z",
  "started_at": "2026-08-23T08:00:01Z",
  "finished_at": null
}
```

가능한 최종 상태:

- `COMPLETED`
- `FAILED`

외부 API 하나가 일부 실패해도 전체 실행이 가능한 경우 `COMPLETED`로 끝나며 `warnings`와 `source_status`에 degraded 원인이 기록됩니다.

### 3. 해당 실행의 고정 결과 조회

```http
GET /api/v1/ranking-runs/{run_id}/result?limit=100
X-Internal-API-Key: <INTERNAL_API_KEY>
```

이 엔드포인트는 해당 실행 시점의 **자동 분석 결과 스냅샷**을 반환합니다. 다른 배치나 수동 수정의 영향을 받지 않으므로 백엔드가 자신이 시작한 작업의 결과를 정확히 가져올 때 사용합니다.

```json
{
  "run_id": "6cd61210-7a14-4dd0-a85c-364dd24a61a5",
  "status": "COMPLETED",
  "generated_at": "2026-08-23T08:08:31Z",
  "count": 100,
  "warnings": [
    "naver_news: quota exhausted; continued without news signal"
  ],
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

작업이 아직 완료되지 않았다면 `409 Conflict`를 반환합니다.

### 4. 현재 서비스용 결과 조회

```http
GET /api/v1/challenges?limit=100
X-Internal-API-Key: <INTERNAL_API_KEY>
```

이 엔드포인트는 가장 최근 랭킹에 수동 override까지 반영한 **현재 유효 결과**를 반환합니다.

```json
{
  "generated_at": "2026-08-23T08:08:31Z",
  "count": 100,
  "results": [
    {
      "id": "bad-challenge",
      "rank": 1,
      "name": "BAD 챌린지",
      "representative_youtube_url": "https://www.youtube.com/watch?v=AAA",
      "guide_youtube_url": "https://www.youtube.com/watch?v=BBB",
      "automatic_rank": 1,
      "automatic_score": 91.4,
      "lifecycle": "RISING",
      "kr_affinity": 0.91,
      "confidence": 0.87,
      "category": "dance",
      "active": true,
      "rank_overridden": false,
      "name_overridden": false,
      "representative_video_overridden": false,
      "guide_video_overridden": false,
      "updated_at": "2026-08-23T08:08:31Z"
    }
  ]
}
```

### 실행별 결과와 현재 결과의 차이

| API | 의미 |
|---|---|
| `/ranking-runs/{run_id}/result` | 특정 실행의 변경되지 않는 자동 결과 |
| `/challenges` | 최신 자동 결과 + 운영자가 적용한 수동 override |

백엔드가 배치 실행 직후 결과를 수집할 때는 실행별 결과 API를 사용하고, 앱에 항상 최신 상태를 제공할 때는 현재 결과 API를 사용합니다.

전체 연동 계약과 Spring WebClient 예시는 [`docs/BACKEND_INTEGRATION.md`](docs/BACKEND_INTEGRATION.md)에 있습니다.

## 수동 수정

메인 백엔드 또는 운영 도구는 랭킹, 이름, 대표영상, 가이드영상 링크를 수정할 수 있습니다.

```http
PATCH /api/v1/challenges/{challenge_id}
X-Internal-API-Key: <INTERNAL_API_KEY>
Content-Type: application/json

{
  "rank": 3,
  "name": "BAD 댄스 챌린지",
  "representative_youtube_url": "https://www.youtube.com/watch?v=NEW1",
  "guide_youtube_url": "https://www.youtube.com/watch?v=NEW2"
}
```

수정값은 override 컬럼에 저장되어 다음 자동 배치가 덮어쓰지 않습니다. 필드에 `null`을 명시하면 해당 override를 해제하고 자동 분석값으로 돌아갑니다.

JSON 파일로 일괄 수정할 수도 있습니다.

```bash
python -m app.cli import-overrides data/ranking_overrides.json
```

DB가 원본이며 JSON은 import/export 인터페이스입니다. HTML 랭킹 파일은 생성하지 않습니다.

## 영상 선정 기준

### 앱 화면용 대표영상

유명하고 조회수가 높은 실제 참여 영상을 우선합니다.

| 기준 | 비중 |
|---|---:|
| 조회수/유명도 | 45% |
| 챌린지 관련성 | 25% |
| 실제 참여/시범 여부 | 10% |
| 국내 관련성 | 10% |
| 최신성 | 5% |
| 참여율 | 5% |

### 따라하기용 가이드영상

사용자가 실제로 동작을 보고 따라 하기 쉬운 영상을 우선합니다.

| 기준 | 비중 |
|---|---:|
| 안무/튜토리얼/동작 명료도 | 45% |
| 챌린지 관련성 | 20% |
| 실제 참여/시범 여부 | 10% |
| 국내 관련성 | 10% |
| 조회수 | 5% |
| 최신성 | 5% |
| 참여율 | 5% |

`tutorial`, `튜토리얼`, `거울모드`, `mirrored`, `slow`, `천천히`, `step by step`, `안무영상`, `dance practice`, `choreography`, `연습영상`, `전체 안무`, `시범` 표현에 가점을 줍니다. 전용 가이드가 없으면 동작이 잘 보이는 실제 참여 영상으로 fallback합니다.

YouTube 공개 Data API에는 일반 영상 `shareCount`가 없으므로 참여율은 `likeCount`, `commentCount`, `viewCount` 중심으로 계산합니다.

초기 단일 영상 선정 기준은 관련성 40%, 실제 참여 여부 20%, 조회 성과 15%, 최신성 10%, 참여율 5%, 국내 관련성 10%였습니다. 가중합 전 관련성 미달 및 `COMMENTARY`, `UNRELATED` 영상을 제거합니다. 목적은 단순히 가장 인기 있는 영상이 아니라, 사용자가 클릭했을 때 해당 국내 유행 챌린지를 가장 빠르게 이해할 수 있는 실제 참여 영상을 고르는 것이며, 이 원칙은 현재 가이드영상 선정에 계승됩니다.

## Apify 기반 Instagram Discovery

YouTube 중심 Discovery는 게임 미션, 장기 목표, 유튜버 자체 기획, 해설 영상 등 YouTube 스타일 콘텐츠에 편향됩니다. 따라서 Instagram을 후보 발견의 중심으로 사용합니다.

1. Instagram Search Scraper의 popular reels를 내부 seed keyword로 탐색
2. 발견한 hashtag/audio를 자동 확장해 재탐색
3. 필요 시 Hashtag Scraper와 Reel Scraper로 고유 크리에이터 수, 음원 반복, 최근 게시 증가, 실제 행동 유사성 검증

주요 개선 대상은 릴스형 춤, 포즈, 손동작, 전환, 변신, 특정 음원, 이름 없는 초기 챌린지입니다.

Apify는 Meta 공식 Trend API가 아니며 keyword seed가 필요하고 데이터 필드가 불안정할 수 있습니다. 한국 audience geo를 직접 보장하지 않으므로 한국어 caption/ASR/hashtag, creator/location, NAVER/YouTube 신호를 결합합니다. Meta 자동수집 약관과 상업적 사용 리스크는 별도 검토해야 하며, 수집부는 향후 허가된 공급사나 공식 접근 방식으로 교체할 수 있는 Adapter 경계를 유지합니다.

## 프로젝트 구조

```text
app/
├─ agents/
│  ├─ registry.py
│  ├─ challenge_ranking/
│  ├─ shortform/
│  └─ editing/
├─ api/v1/
│  ├─ agents.py
│  ├─ challenges.py
│  ├─ ranking_runs.py
│  ├─ shortform_sessions.py
│  ├─ editing_runs.py
│  ├─ overrides.py
│  └─ health.py
├─ core/
├─ db/
├─ models/
├─ schemas/
├─ services/
├─ workers/
└─ ranker_core/

reals-video-engine/
├─ reals_edit_engine/
├─ demo/
├─ registry/
└─ tools/
```

- `app/agents`: Agent 단위의 외부 경계와 등록 정보
- `app/ranker_core`: 현재 challenge-ranking Agent의 분석 엔진
- `app/services`: DB 저장과 애플리케이션 서비스
- `app/workers`: Celery 비동기 실행
- `app/api`: 메인 백엔드가 호출하는 내부 REST API

구조 원칙은 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)에 정리되어 있습니다.

## 실행

### Docker Compose

```bash
cp .env.example .env
# .env에 INTERNAL_API_KEY와 외부 API 키 입력
docker compose up --build -d
```

서비스:

- API: `http://localhost:8000`
- Swagger: `http://localhost:8000/docs`
- PostgreSQL
- Redis
- Celery Worker
- Celery Beat

### 필수 환경변수

```dotenv
INTERNAL_API_KEY=replace-with-a-long-random-value
APIFY_API_TOKEN=
GEMINI_API_KEY=
YOUTUBE_API_KEY=
NAVER_API_HUB_CLIENT_ID=
NAVER_API_HUB_CLIENT_SECRET=
```

실제 `.env`는 커밋하지 않습니다.

### 로컬 개발

```bash
python -m venv .venv
source .venv/bin/activate
# Windows PowerShell: .\.venv\Scripts\Activate.ps1

pip install -e ".[dev]"
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
```

테스트:

```bash
ruff check .
pytest
```

외부 API 연결 확인:

```bash
python scripts/check_apis.py
```

동기 배치 실행:

```bash
python -m app.cli run-ranking
```

## 장애 처리

- Apify 일부 seed 실패: 해당 seed만 건너뜀
- Gemini 429/실패: 제한 재시도 후 확보된 데이터로 degraded 실행
- YouTube 429: 추가 영상 검색을 중단하고 랭킹은 저장
- NAVER 실패: 해당 지표만 누락 처리
- 긴 분석은 FastAPI 요청 프로세스가 아니라 Celery Worker에서 실행
- 부분 실패는 `warnings`와 `source_status`로 백엔드에 전달

## API 상태 코드

| 상태 | 의미 |
|---:|---|
| `200` | 조회 또는 수정 성공 |
| `202` | 랭킹 작업 생성 성공 |
| `401` | 내부 API 키 누락 또는 불일치 |
| `404` | 실행 또는 챌린지 없음 |
| `409` | 완료되지 않은 실행의 결과 요청 |
| `422` | 랭킹 실행에 필요한 외부 API 키 누락 |
| `503` | 운영 환경에서 내부 API 키 미설정 |

## 주요 엔드포인트

| Method | Endpoint | 설명 |
|---|---|---|
| GET | `/api/v1/health/live` | 프로세스 상태 |
| GET | `/api/v1/health/ready` | DB와 외부 API 키 준비 상태 |
| GET | `/api/v1/agents` | 현재 구현된 Agent 목록 |
| POST | `/api/v1/ranking-runs` | 챌린지 랭킹 분석 시작 |
| GET | `/api/v1/ranking-runs/{id}` | 분석 진행 상태 |
| GET | `/api/v1/ranking-runs/{id}/result` | 해당 실행의 고정 결과 |
| POST | `/api/v1/shortform-sessions` | 숏폼 brief 대화 세션 시작 |
| POST | `/api/v1/shortform-sessions/{id}/turns` | 숏폼 대화 진행 |
| POST | `/api/v1/editing-runs` | 비동기 영상 편집 실행 시작 |
| GET | `/api/v1/editing-runs/{id}` | 편집 진행 상태 |
| GET | `/api/v1/editing-runs/{id}/result` | EditRecipe·렌더·게시 문구 결과 |
| POST | `/api/v1/editing-runs/{id}/revisions` | 기존 결과를 보존한 수정 run 생성 |
| GET | `/api/v1/challenges?limit=100` | 최신 유효 Top 100 |
| GET | `/api/v1/challenges/{id}` | 챌린지 상세 |
| PATCH | `/api/v1/challenges/{id}` | 랭킹·이름·영상 수동 override |
| POST | `/api/v1/overrides/import` | JSON override 일괄 반영 |

## 문서

- [아키텍처와 서비스 경계](docs/ARCHITECTURE.md)
- [메인 백엔드 연동 계약](docs/BACKEND_INTEGRATION.md)
- [새 Agent 추가 방법](docs/ADDING_AN_AGENT.md)
- [REALS 숏폼 영상 편집 엔진](reals-video-engine/README.md)

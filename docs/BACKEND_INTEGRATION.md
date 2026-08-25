# 메인 백엔드 연동 계약

## 전제

AI 서버는 독립 서비스다. 메인 백엔드가 내부 네트워크 또는 허용된 서비스 도메인을 통해 호출한다.

```text
Main Backend → FLY AI Service
```

앱 클라이언트가 직접 호출하지 않는다.

## 인증

health endpoint를 제외한 내부 API에 다음 헤더를 보낸다.

```http
X-Internal-API-Key: <INTERNAL_API_KEY>
```

메인 백엔드와 AI 서버의 환경변수 값이 같아야 한다.

## 권장 호출 순서

### 1. 분석 요청

```http
POST /api/v1/ranking-runs
X-Internal-API-Key: <INTERNAL_API_KEY>
```

응답:

```json
{
  "run_id": "6cd61210-7a14-4dd0-a85c-364dd24a61a5",
  "status": "QUEUED",
  "task_id": "f1018f72-c4dc-4e48-89d0-f9678a935845"
}
```

HTTP status는 `202 Accepted`다. 분석 완료 응답이 아니라 작업 접수 응답이다.

현재 idempotency key와 callback webhook은 구현되어 있지 않다. 메인 백엔드는 중복 POST를 방지하고 상태 API를 polling한다.

### 2. 상태 polling

```http
GET /api/v1/ranking-runs/{run_id}
X-Internal-API-Key: <INTERNAL_API_KEY>
```

권장 polling 간격은 수 초 단위이며, 메인 백엔드에서 최대 대기 시간과 재시도 정책을 둔다.

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

처리 규칙:

- `QUEUED`, `RUNNING`: 계속 polling
- `COMPLETED`: 실행별 결과 조회
- `FAILED`: `error_message`를 기록하고 실패 처리

### 3. 실행별 결과 수집

```http
GET /api/v1/ranking-runs/{run_id}/result?limit=100
X-Internal-API-Key: <INTERNAL_API_KEY>
```

```json
{
  "run_id": "6cd61210-7a14-4dd0-a85c-364dd24a61a5",
  "status": "COMPLETED",
  "generated_at": "2026-08-23T08:08:31Z",
  "count": 100,
  "warnings": [],
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

이 결과는 해당 run의 immutable automatic snapshot이다. 메인 백엔드가 결과를 자체 DB에 저장할 때 적합하다.

### 4. 현재 유효 랭킹 조회

```http
GET /api/v1/challenges?limit=100
X-Internal-API-Key: <INTERNAL_API_KEY>
```

이 API는 최신 자동 결과와 수동 override를 합친 현재 상태다. 앱 요청 때 최신 AI 서버 상태를 직접 읽거나, 백엔드 캐시를 갱신할 때 사용한다.

## 대표영상과 가이드영상

- `representative_youtube_url`: 앱 카드와 상세 상단에 사용할 유명한 대표 참여 영상
- `guide_youtube_url`: 사용자가 동작을 보고 따라 할 수 있는 튜토리얼·안무·명확한 시범 영상

링크가 없는 경우 값은 `null`이다. 백엔드는 빈 문자열 대신 `null`을 처리해야 한다.

## 수동 override

```http
PATCH /api/v1/challenges/{challenge_id}
X-Internal-API-Key: <INTERNAL_API_KEY>
Content-Type: application/json
```

```json
{
  "rank": 3,
  "name": "BAD 댄스 챌린지",
  "representative_youtube_url": "https://www.youtube.com/watch?v=NEW1",
  "guide_youtube_url": "https://www.youtube.com/watch?v=NEW2"
}
```

요청에 포함된 필드만 변경한다. 특정 필드를 `null`로 보내면 그 필드의 override를 해제한다.

## 오류 계약

| HTTP | 상황 | 백엔드 처리 |
|---:|---|---|
| 401 | 내부 API 키 누락·불일치 | 설정 오류로 처리, 재시도 금지 |
| 404 | run/challenge 없음 | ID 또는 데이터 동기화 확인 |
| 409 | 완료 전 run result 요청 | 상태 polling 계속 |
| 422 | 외부 API 키 미설정 | AI 서버 환경설정 오류 |
| 503 | 운영 환경에서 내부 키 미설정 | AI 서버 배포 설정 오류 |
| 5xx | 예기치 않은 서버 오류 | 제한 재시도 후 장애 기록 |

Apify, Gemini, YouTube, NAVER의 부분 실패는 가능한 경우 HTTP 오류 대신 `COMPLETED` 결과의 `warnings`와 `source_status`에 기록된다.

## 숏폼 Agent

백엔드는 매장·메뉴·상권 context를 포함해 세션을 만들고, 반환된 `session_id`로 사용자 입력을
한 turn씩 전달한다. 사용자가 brief를 확인한 뒤에만 ACTIVE 영상편집DB 버전 1개가 추천된다.
정확히 맞는 후보가 없어도 AI 서버가 조건을 단계적으로 완화하므로 추천 응답은 항상 1개다.
모든 후보를 이미 보여준 경우에는 새 추천 주기를 시작하며, 추천 선택 LLM 장애 시에도 AI
서버가 안정적인 후보 하나를 반환한다.

```http
POST /api/v1/shortform-sessions
X-Internal-API-Key: <INTERNAL_API_KEY>
Content-Type: application/json

{
  "store_context": {
    "store": {
      "store_id": "store_123",
      "store_name": "사릴스 카페",
      "category": "카페"
    },
    "representative_menus": [
      {"menu_id": "menu_001", "name": "딸기 크림 라떼", "price": 6500}
    ],
    "trade_area": {
      "characteristics": ["주말 방문 비중이 높음"],
      "target_age_ranges": ["20대", "30대"]
    }
  }
}
```

대화 입력은 `TEXT`, `OPTION`, `CONFIRM` 중 하나다.

```http
POST /api/v1/shortform-sessions/{session_id}/turns
Content-Type: application/json

{"input":{"type":"TEXT","text":"딸기 크림 라떼를 홍보하고 싶어요"}}
```

```http
POST /api/v1/shortform-sessions/{session_id}/turns
Content-Type: application/json

{"input":{"type":"OPTION","option_id":"option_from_previous_response"}}
```

```http
POST /api/v1/shortform-sessions/{session_id}/turns
Content-Type: application/json

{"input":{"type":"CONFIRM","value":true}}
```

응답의 `action`은 `ASK`, `SAVE_AND_ASK`, `CLARIFY`, `SUGGEST_SWITCH`,
`RESOLVE_CONFLICT`, `CONFIRM`, `RECOMMEND` 중 하나다. 추천 응답에는
`recommendation_id`, `project_title`, `title`, `concept`, `video_editing_db_id`,
`video_editing_db_version`이 포함된다. 백엔드는 사용자가 추천을 수락하면 이 식별자와 버전을
프로젝트에 저장한다.

다시 추천받기는 현재 context를 유지하고 이미 노출한 DB를 제외한다.

```http
POST /api/v1/shortform-sessions/{session_id}/recommendations/next
```

촬영 가이드는 선택된 정확한 DB 버전으로 조회한다.

```http
GET /api/v1/video-editing-db/{record_id}/versions/{version}/shooting-guide
```

새로고침이나 프로젝트 취소로 대화를 폐기할 때는 세션을 삭제한다.

```http
DELETE /api/v1/shortform-sessions/{session_id}
```

## 편집 Agent

편집은 Celery 비동기 run으로 처리한다. 입력 영상편집DB는 반드시 `ACTIVE`인 정확한 버전이어야 하며 영상은 HTTP(S) 서명 URL로 전달한다.

```http
POST /api/v1/editing-runs
X-Internal-API-Key: <INTERNAL_API_KEY>
Content-Type: application/json
```

```json
{
  "project": {
    "project_id": "project_123",
    "store_id": "store_123",
    "promotion_subject": {"type": "MENU", "name": "딸기 크림 라떼", "menu_id": "menu_001"},
    "promotion_objective": "sales",
    "face_exposure": "not_allowed"
  },
  "selected_shortform": {
    "recommendation_id": "rec_123",
    "video_editing_db_id": "video_editing_db_014",
    "video_editing_db_version": 3
  },
  "videos": [
    {
      "video_id": "take_501",
      "footage_url": "https://cdn.example/takes/501.mp4",
      "shooting_scene_order": 1
    }
  ],
  "revision": null
}
```

응답은 `202 Accepted`이며 `run_id`, `status=QUEUED`, `task_id`를 반환한다. 이후 다음 상태 API를 polling한다.

```http
GET /api/v1/editing-runs/{run_id}
```

stage는 `PREPARING_VIDEO_CONTEXT → PLANNING_RECIPE → VALIDATING_RECIPE → RENDERING → COMPLETED` 순서다. 상태가 `COMPLETED` 또는 `SOURCE_GAP`이면 결과를 조회한다.

```http
GET /api/v1/editing-runs/{run_id}/result
```

`COMPLETED` 결과는 `recipe`, `render`, `publishing`, `warnings`를 포함한다. 촬영 근거가 부족한 경우 Agent가 재촬영을 결정하지 않고 아래 형태를 반환한다.

```json
{
  "run_id": "edit_123",
  "status": "SOURCE_GAP",
  "recipe": null,
  "render": null,
  "publishing": null,
  "warnings": [],
  "missing_scene_roles": ["RESULT"],
  "available_options": ["USE_REDUCED_STRUCTURE", "ADD_MORE_VIDEO"]
}
```

자연어 수정은 기존 run을 변경하지 않고 새 immutable child run을 만든다. 영상 URL은
서명 만료가 가능하므로 수정 요청마다 같은 `video_id`와 `shooting_scene_order`에 대한
새 URL을 다시 전달해야 한다. `SOURCE_GAP` run에서는 기존 영상을 보존하면서 새 영상을
추가할 수 있다.

```http
POST /api/v1/editing-runs/{run_id}/revisions
Content-Type: application/json

{
  "revision_action":"첫 장면을 더 짧게 하고 자막을 크게 해줘",
  "videos":[
    {
      "video_id":"take_501",
      "footage_url":"https://cdn.example/takes/501.mp4?refreshed-signature",
      "shooting_scene_order":1
    }
  ]
}
```

AI worker는 MP4 자체를 GPT에 보내지 않는다. `ffprobe` 메타데이터와 타임스탬프 키프레임을 제한적으로 생성하며, DB에는 base64 이미지가 아닌 키프레임 시각만 저장한다. Validator를 통과한 `VIDEO_ONLY` 레시피만 `EDITING_RENDERER_URL/renders`에 전달된다.

Renderer 요청은 `reals-render-job-1.0` 계약을 사용한다. 원격 영상 URL과 메타데이터, 다중 컷의 순서·트림을 담은 `source_assembly`, 엔진 계약과 같은 필드명의 `final_render.edit_recipe`를 함께 보낸다. 단일 컷은 `ONE_TAKE_PASSTHROUGH`, 다중 컷은 정확한 트림 조립 후 `MULTI_CUT_ASSEMBLED`로 처리한다. 저장소의 `app.renderer.main` 서비스가 URL을 로컬 `MediaFileRef.path`로 resolve하고, 필요 시 조립본을 만든 뒤 REALS `FinalRenderRequest`와 native Validator/QC를 실행한다. 성공한 MP4는 `RENDERER_OUTPUT_DIR`에 저장되고 `RENDERER_PUBLIC_BASE_URL/files/...` URL로 반환된다.

AI 측 preflight Validator와 LLM capability는 `EDITING_REALS_REGISTRY_PATH`에 있는 REALS registry bundle을 함께 읽는다. 시작 시 manifest SHA-256을 검증하고, 효과·전환·최소 컷·자막 제한·렌더 프로필을 그 registry에서 가져온다. Renderer 내부의 native Validator는 로컬 파일 범위, 폰트 파일/글리프, 최종 QC를 다시 검증하며 최종 권한을 가진다.

```text
EditRecipe
  → registry-backed preflight + bounded LLM repair
  → RealsRecipeAdapter
  → POST /renders (reals-render-job-1.0)
  → source assembly (multi-cut only)
  → REALS native Validator + FINAL_RENDER + QC
```

## cURL 예시

```bash
AI_BASE_URL=http://localhost:8000
AI_INTERNAL_KEY=replace-with-a-long-random-value

curl -X POST \
  "$AI_BASE_URL/api/v1/ranking-runs" \
  -H "X-Internal-API-Key: $AI_INTERNAL_KEY"
```

```bash
curl \
  "$AI_BASE_URL/api/v1/ranking-runs/$RUN_ID" \
  -H "X-Internal-API-Key: $AI_INTERNAL_KEY"
```

```bash
curl \
  "$AI_BASE_URL/api/v1/ranking-runs/$RUN_ID/result?limit=100" \
  -H "X-Internal-API-Key: $AI_INTERNAL_KEY"
```

## Spring WebClient 예시

```java
WebClient aiClient = WebClient.builder()
    .baseUrl(aiBaseUrl)
    .defaultHeader("X-Internal-API-Key", aiInternalApiKey)
    .build();

Mono<RankingRunCreateResponse> created = aiClient.post()
    .uri("/api/v1/ranking-runs")
    .retrieve()
    .bodyToMono(RankingRunCreateResponse.class);
```

상태 확인:

```java
Mono<RankingRunStatusResponse> status = aiClient.get()
    .uri("/api/v1/ranking-runs/{runId}", runId)
    .retrieve()
    .bodyToMono(RankingRunStatusResponse.class);
```

결과 조회:

```java
Mono<RankingRunResultResponse> result = aiClient.get()
    .uri(uriBuilder -> uriBuilder
        .path("/api/v1/ranking-runs/{runId}/result")
        .queryParam("limit", 100)
        .build(runId))
    .retrieve()
    .bodyToMono(RankingRunResultResponse.class);
```

## 운영 권장사항

- 메인 백엔드가 AI 서버 URL과 내부 키를 환경변수로 관리
- 외부 API 키는 AI 서버에만 저장
- AI 서버 호출에 connect/read timeout 설정
- 중복 실행 방지용 백엔드 lock 적용
- 마지막 성공 랭킹을 백엔드에서 캐시
- 새 run 실패 시 이전 성공 랭킹 유지
- `warnings`와 `source_status`를 운영 로그에 저장

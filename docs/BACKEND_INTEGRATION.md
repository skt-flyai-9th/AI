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

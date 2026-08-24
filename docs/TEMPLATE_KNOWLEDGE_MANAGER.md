# Template Knowledge Manager

Template Knowledge Manager는 `상권분석템플릿`과 `영상편집템플릿`을 같은 버전 수명주기로 관리하는 AI 서버 내부 운영 컴포넌트다. 현재 범위는 AI 서버 구현과 독립 실행까지이며 메인 백엔드 호출 연결은 포함하지 않는다.

## 수명주기

```text
evidence
  → LLM version candidate
  → deterministic schema/product/renderer validation
  → JSON diff
  → human approval (default)
  → immutable new ACTIVE version
  → previous ACTIVE version becomes ARCHIVED
```

현재 버전을 직접 수정하지 않는다. 후보가 만들어진 뒤 base version이 바뀌면 해당 후보는 `BASE_VERSION_STALE`로 무효화된다. 후보 상태는 `GENERATED`, `VALIDATED`, `INVALID`, `APPROVED`, `REJECTED`, `APPLIED` 중 하나다.

기본 설정인 `TEMPLATE_REQUIRE_HUMAN_APPROVAL=true`에서는 검증을 통과해도 승인 전까지 ACTIVE 템플릿이 변경되지 않는다.

## 상권분석템플릿

상권 템플릿은 업종 범위, 상권 유형, 분석 차원, 집계 근거 기반 추론 규칙, 추천 힌트, 정책을 버전별로 저장한다.

결정론적 검증은 다음을 강제한다.

- 집계 데이터만 사용
- 개인의 실제 속성 추론 금지
- 민감 속성 추론 금지
- 최소 표본과 불확실성 정책 명시
- 중복 차원·규칙 ID 금지

GPT는 신규 상권 근거와 현재 템플릿을 비교해 다음 버전 후보를 만든다. ACTIVE 템플릿을 실제 근거에 적용한 결과는 `trade_area_analyses`에 근거 스냅샷과 함께 저장된다.

## 영상편집템플릿

영상편집템플릿 갱신은 PostgreSQL의 활성 Trend Research 결과를 선행 근거로 사용한다.

```text
Challenge / Trend Research
  → representative public YouTube URL
  → Gemini native video understanding
  → persisted timestamped editing insight
  → GPT editing-template candidate
  → REALS registry/product validation
```

Gemini 분석은 공개 `https` YouTube URL만 허용하며, 같은 trend/url/model 조합은 캐시한다. 영상 인사이트에는 훅, 장면 순서, 페이싱, 자막, 카메라, 전환, 오디오 역할, 관찰 근거와 신뢰도가 포함된다.

편집 템플릿 검증은 다음을 코드로 차단한다.

- TTS 및 합성 내레이션
- 사진 타임라인 및 photo-to-video
- `VIDEO_ONLY` 이외의 소스
- REALS registry에 없는 효과·전환·렌더·safe-area·assembly profile
- `SILENT_V1` 이외의 오디오 정책
- registry보다 짧은 최소 컷 또는 render profile보다 긴 영상
- Trend Research 근거 없는 기존 템플릿 업데이트

## 초기 운영 라이브러리

`python -m app.cli seed-template-library`는 검증된 초기 버전을 idempotent하게 생성한다.

- 상권분석템플릿 6개: 오피스, 주거, 대학가, 역세권, 관광, 일반
- 영상편집템플릿 6개: 메뉴 결과, 제조 과정, 공간 소개, 혜택 안내, 사장님 추천, 서비스 전후

초기 데이터만 `SYSTEM_AUTO` bootstrap 후보로 적용된다. 이후 업데이트는 일반 후보·검증·승인 수명주기를 거친다.

## 독립 실행

```bash
alembic upgrade head
python -m app.cli seed-template-library
python -m app.cli generate-editing-template edit_menu_reveal --trend-id <trend_id>
python -m app.cli generate-trade-area-template trade_area_office evidence.json
python -m app.cli approve-template-candidate <candidate_id> <reviewer>
python -m app.cli analyze-trade-area evidence.json --template-id trade_area_office
```

AI 서버 내부 운영 API도 `/api/v1/template-knowledge` 아래에 준비되어 있다. Gemini/GPT를 호출하는 생성·분석 요청은 `202 Accepted`와 `run_id`를 반환하고 Celery worker가 실행한다. `/runs/{run_id}`를 polling한 뒤 `/runs/{run_id}/result`에서 결과를 조회한다.

| Method | Endpoint | 역할 |
|---|---|---|
| POST | `/bootstrap` | 초기 운영 템플릿 idempotent 생성 |
| GET | `/templates` | 버전·상태 조회 |
| GET | `/candidates` | 후보·diff·검증·근거 조회 |
| POST | `/trade-area/candidates` | 상권 템플릿 후보 생성 |
| POST | `/video-editing/candidates` | Trend/Gemini 기반 편집 템플릿 후보 생성 |
| GET | `/runs/{id}` | 생성·분석 run 상태 조회 |
| GET | `/runs/{id}/result` | 완료된 run 결과 조회 |
| POST | `/candidates/{id}/validate` | 후보 재검증 |
| POST | `/candidates/{id}/approve` | 승인 후 새 ACTIVE 버전 적용 |
| POST | `/candidates/{id}/reject` | 후보 거절 |
| POST | `/trade-area/analyze` | ACTIVE 템플릿으로 상권 근거 분석 run 생성 |

모든 운영 API는 `X-Internal-API-Key`를 요구한다. 메인 백엔드에서 이 API를 호출하는 연결 작업은 별도 범위다.

## 주기 실행

Celery Beat에는 `weekly-template-maintenance`가 등록되어 있다. 기본값은 비활성화다.

```dotenv
TEMPLATE_MAINTENANCE_ENABLED=false
TEMPLATE_MAINTENANCE_WEEKDAY=0
TEMPLATE_MAINTENANCE_HOUR_KST=5
TEMPLATE_MAINTENANCE_MINUTE_KST=0
```

활성화하면 ACTIVE 영상편집템플릿별로 미처리 후보가 없는 경우 새 Trend Research 결과를 사용해 검토 후보를 만든다. 자동 승인하지 않는다. 상권 갱신은 새로운 외부 집계 근거가 필요하므로 CLI/API로 근거를 명시해 실행한다.

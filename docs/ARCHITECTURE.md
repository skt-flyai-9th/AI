# AI 서버 아키텍처

## 서비스 경계

이 저장소는 메인 백엔드에 포함되는 라이브러리가 아니라 별도로 배포되는 AI 마이크로서비스다.

```text
Client
  → Main Backend
    → AI Service
      → External AI/Data APIs
```

클라이언트는 AI 서버를 직접 호출하지 않는다. 사용자 인증, 서비스 도메인 로직, 앱 응답 조합은 메인 백엔드가 책임진다. AI 서버는 분석 작업과 AI 결과만 책임진다.

## 현재 범위

현재 실제 구현된 Agent는 세 개다.

- 입력: API 키와 메인 백엔드의 실행 요청
- 처리: Instagram 후보 발견, Gemini 분석, NAVER 국내성 검증, YouTube 영상 선정
- 출력: 챌린지 Top 100과 대표영상·가이드영상 링크

- `shortform`: 대화형 brief 수집과 ACTIVE 편집 템플릿 추천
- `editing`: 영상 컨텍스트 생성, EditRecipe 계획·검증·repair, Renderer 연동
- `Template Knowledge Manager`: 상권/영상편집 템플릿 후보 생성, Gemini 참고영상 분석, diff·검증·승인·버전 활성화

## Agent 구조

```text
app/agents/
├─ registry.py
├─ challenge_ranking/
├─ shortform/
└─ editing/

app/template_knowledge/
├─ llm.py
├─ validation.py
├─ service.py
├─ maintenance.py
├─ source_library.py
├─ sources/          # 사용자 제공 Excel + canonical JSON
└─ seeds.py          # import 호환 alias, 합성 seed 없음
```

`registry.py`는 현재 제공 가능한 Agent와 API 계약을 노출한다. 각 Agent는 외부 API 라우터가 내부 구현 세부사항에 직접 의존하지 않도록 서비스 경계를 제공한다.

현재 분석 엔진은 `app/ranker_core`에 위치한다. `app/agents/challenge_ranking/service.py`가 애플리케이션 서비스와 엔진을 감싸는 공개 경계다. 향후 내부 구현을 이동해도 API 라우터와 worker는 Agent 서비스 경계만 유지하면 된다.

## 계층

### API

`app/api/v1`은 메인 백엔드와의 HTTP 계약만 담당한다.

- 인증
- 입력 검증
- 상태 코드
- 응답 스키마
- Celery 작업 enqueue

외부 데이터 수집이나 모델 추론을 HTTP 요청 안에서 직접 수행하지 않는다.

### Agent

`app/agents`는 기능 단위 경계다.

- Agent 등록
- Agent의 실행 서비스
- 실행별 결과 조회
- 향후 Agent별 계약 분리

### Services

`app/services`는 DB 저장, override, export와 같은 애플리케이션 로직을 담당한다.

### Engine

`app/ranker_core`는 challenge-ranking Agent의 데이터 수집·특징 계산·점수화·영상 선정 엔진이다.

### Worker

`app/workers`는 Celery에서 장시간 작업을 실행한다.

### Database

AI 서버 DB는 다음을 저장한다.

- pipeline run
- challenge current state
- ranking snapshot
- manual override
- source status and warnings
- shortform session and confirmed brief state
- editing run, immutable revision lineage, recipe, render result
- trade-area/editing template immutable versions and activation lineage
- user-provided template source bundles, checksums, dataset manifests and row records
- template update candidates, diffs, validation and approval audit
- Gemini reference-video insights and trade-area analysis evidence snapshots

메인 백엔드의 사용자·매장·프로젝트 데이터는 저장하지 않는다.

Template Knowledge Manager의 상세 수명주기와 독립 실행 방법은 `docs/TEMPLATE_KNOWLEDGE_MANAGER.md`를 참고한다. 메인 백엔드 호출 연결은 별도 범위다.

## 결과 모델

AI 서버는 두 종류의 결과를 제공한다.

### Run snapshot

`GET /api/v1/ranking-runs/{run_id}/result`

특정 실행의 자동 분석 결과다. 실행 이후 수동 수정이나 다른 배치의 영향을 받지 않는다.

### Current effective ranking

`GET /api/v1/challenges`

가장 최근 자동 분석 결과에 수동 override를 반영한 현재 상태다.

이 구분을 통해 재현성과 운영 수정 가능성을 모두 확보한다.

## 인증

운영 환경에서 health endpoint를 제외한 내부 API는 `X-Internal-API-Key`를 요구한다.

```http
X-Internal-API-Key: <shared secret>
```

이 값은 메인 백엔드와 AI 서버에만 저장한다. 앱 클라이언트, 프론트엔드 번들, 공개 저장소에 노출하지 않는다.

초기 버전 호환을 위한 `X-Admin-Token`은 deprecated alias다.

## 확장 원칙

새 Agent는 다음 조건을 만족해야 한다.

1. `app/agents/<agent_id>`에 독립 경계를 가진다.
2. `app/agents/registry.py`에 등록한다.
3. 장시간 처리는 Celery task로 실행한다.
4. 실행 상태와 결과 계약을 명시한다.
5. 외부 API 실패 시 전체 서버가 중단되지 않도록 격리한다.
6. 메인 백엔드와 공유하는 API는 버전이 명확해야 한다.
7. 다른 Agent의 모델·테이블·설정을 암묵적으로 수정하지 않는다.

자세한 절차는 `docs/ADDING_AN_AGENT.md`를 참고한다.

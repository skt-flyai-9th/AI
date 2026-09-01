# Agent 실행 하네스

이 문서는 REALS AI 서버에 이미 존재하는 세 Agent의 공통 실행 경계를 설명한다.
하네스 도입으로 새로운 Agent가 등록되거나 기존 Agent의 업무 범위가 바뀌지는 않는다.

## 적용 대상

| Agent | 실행 엔진 | 하네스 operation | 상관관계 ID |
|---|---|---|---|
| `challenge-ranking` | 리서치·랭킹 파이프라인 | `research` | `PipelineRun.id` |
| `shortform` | LangGraph | `turn`, `recommend` | `ShortformSession.id` |
| `editing` | LangGraph | `plan`, `reduced_plan` | `EditingRun.id` |

트렌드 리서치 Agent는 1~100위 전체를 리서치한다. 성공한 실행은 검증된 Top 100으로
현재 활성 랭킹 전체를 교체하며, 별도 승인이나 승인 대기 상태 없이 즉시 활성화한다.
100개가 모두 준비되기 전에는 기존 DB와 공개 `trendcluster.json`을 변경하지 않는다.
하네스는 이 정책과 `run_pipeline` 실행 경계를 함께 검증한다.

## 실행 계약

하네스는 operation별 필수 입력과 필수 출력을 실행 전후에 검증한다. 필수 필드 확인 뒤
Agent별 의미 validator를 실행하며, 안전한 repair 실행기가 명시된 작업만 제한된
`실행 → 검증 → repair → 재검증` 루프를 사용한다.

- `challenge-ranking.research`: 런타임 경로·랭킹·소스 설정을 받고 실행 ID, 랭킹,
  소스 지표, 소스 상태를 반환해야 한다. 중복되지 않는 목표 개수의 소셜 트렌드와
  대표·가이드 YouTube URL이 있는지 최종 검증한다.
- `shortform.turn`: 대화 모드, 도메인·매장·프로젝트 문맥, 대화, 사용자 입력과 사진
  목록을 받고 구조화된 `decision`을 반환해야 한다. 의미 검증 실패 시 side effect가
  없는 LangGraph를 한 번 더 실행하고 다시 검증한다.
- `shortform.recommend`: 추천 모드와 영상편집DB 후보를 받고 구조화된
  `recommendations`를 반환해야 한다. 추천 키가 입력 후보 집합 안에 있는지도 검증하고,
  실패하면 한 번 재생성한 뒤 기존 결정론적 fallback으로 넘긴다.
- `editing.plan`, `editing.reduced_plan`: 프로젝트·영상·템플릿 문맥과 진행률·분석
  checkpoint callback을 받고 `decision`과 `validation_passed`를 반환해야 한다. Recipe
  의미 검증과 repair 반복은 기존 Editing LangGraph 내부에서 수행하고, 하네스는 최종
  검증 통과 또는 `exhausted` 상태를 확인한다.

필드 계약 위반은 `AgentHarnessContractError`, 의미 검증 소진은
`AgentHarnessValidationError`로 실패한다. 하네스는 원래 Agent 예외를 다른 예외로
바꾸지 않으며 서비스 계층의 기존 실패 처리와 fallback이 그대로 동작한다.

## Agent별 검증 루프

- 트렌드 리서치: 후보 발견 및 YouTube 재검색 단계에서 실패 후보를 버리고 후순위
  후보를 찾는다. 하네스 최종 gate에서 100개와 각 후보의 대표·가이드 URL을 다시
  확인한다. 전체 외부 수집은 하네스가 자동 재실행하지 않는다.
- 숏폼 추천: 구조화 결과와 후보 소속을 검증하고 실패 시 LangGraph를 최대 한 번
  재실행한다. 다시 실패하면 서비스의 기존 fallback 또는 오류 처리로 이동한다.
- 편집: `plan_recipe → validate_recipe → repair_recipe → validate_recipe`의 기존
  LangGraph 루프를 사용한다. 설정된 수정 횟수가 소진되면 `exhausted`로 종료한다.

## 실행 이벤트

각 호출은 payload를 기록하지 않고 다음 메타데이터만 구조화 로그에 남긴다.

- `phase`: `STARTED`, `VALIDATION_FAILED`, `REPAIR_STARTED`, `SUCCEEDED`, `FAILED`
- `agent_id`, `operation`
- `correlation_id`, 호출별 `invocation_id`
- 완료 또는 실패 시 `duration_ms`
- 실패 시 예외 메시지가 아닌 `error_type`
- 의미 검증 시 `validation_attempt`과 민감하지 않은 `issue_codes`

따라서 매장 정보, 사용자 대화, 사진 URL, 영상 프레임과 프롬프트 본문은 하네스 로그에
포함되지 않는다.

## 하네스가 소유하지 않는 것

- 비용이 크거나 side effect가 있는 전체 Agent 실행 자동 재시도
- PostgreSQL 트랜잭션과 상태 변경
- Celery 작업 제한시간과 중복 작업 방지
- Editing 분석 checkpoint의 실제 저장
- LLM·외부 커넥터별 재시도
- Agent별 결정론적 검증과 fallback

이 책임은 기존 서비스, Worker, LLM 클라이언트와 파이프라인에 유지된다. 하네스 repair
루프는 명시적으로 허용된 side effect 없는 숏폼 LangGraph에만 적용되므로 비용이 큰
리서치나 렌더링이 암묵적으로 중복 실행되지 않는다.

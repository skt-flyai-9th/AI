# 새 Agent 추가 방법

현재 구현된 Agent는 `challenge-ranking`, `shortform`, `editing` 세 개다. 새 Agent는 실제
요구사항과 API 계약이 확정된 뒤 추가한다. 상권분석DB와 영상편집DB의 수명주기는 Agent가
아닌 `Database Knowledge Manager`가 담당한다.

## 권장 디렉터리

```text
app/agents/<agent_id>/
├─ __init__.py
├─ service.py
└─ README.md
```

복잡한 Agent는 내부에 다음 계층을 둘 수 있다.

```text
app/agents/<agent_id>/
├─ domain/
├─ connectors/
├─ schemas/
├─ services/
└─ tasks.py
```

공용 인프라만 `app/core`, `app/db`, `app/workers`에 둔다. 다른 Agent의 구현 디렉터리를 직접 참조하지 않는다.

## 등록

`app/agents/registry.py`에 Agent 정의를 추가한다.

필수 정보:

- 고유 `id`
- 표시 이름
- 상태
- 역할 설명
- 실행 endpoint
- 상태 endpoint
- 결과 endpoint
- 문서 경로

구현되지 않은 Agent를 `AVAILABLE`로 등록하지 않는다.

## API 원칙

- 메인 백엔드가 호출하는 서버 간 API
- `X-Internal-API-Key` 인증
- 장시간 작업은 `202 Accepted`와 run ID 반환
- 상태 polling endpoint 제공
- 실행별 immutable result endpoint 제공
- 현재 유효 결과가 필요하면 별도 endpoint 제공
- 응답 스키마는 Pydantic 모델로 고정
- breaking change는 API version을 변경

## Worker 원칙

모델 추론, 외부 API 수집, 장시간 분석은 FastAPI request process에서 실행하지 않는다. Celery Worker에 enqueue한다.

작업에는 다음 상태가 있어야 한다.

```text
QUEUED → RUNNING → COMPLETED
                   ↘ FAILED
```

`stage`, `progress`, `warnings`, `source_status`, `error_message`를 저장한다.

## 장애 격리

새 Agent의 외부 API 실패가 다른 Agent나 서버 전체를 중단시키면 안 된다.

- timeout
- 최대 재시도
- quota exhausted fast-fail
- partial result
- warning 기록
- 마지막 성공 결과 유지

## 데이터 모델

Agent 전용 데이터는 명확한 테이블 또는 namespace를 사용한다. 메인 백엔드의 도메인 데이터를 복제하지 않는다.

실행별 결과는 snapshot으로 보존하고 현재 결과와 분리하는 것을 권장한다.

## 테스트

최소 테스트:

1. Agent registry 노출
2. 내부 인증
3. 작업 생성 응답
4. 상태 전이
5. 완료 전 결과 요청의 409
6. 완료 결과 스키마
7. 외부 API 부분 실패
8. 수동 override가 있는 경우 자동 배치 보호

# 분석 이력 및 저장소 보존 정책

## 저장 위치

이 AI 서버는 결과를 세 종류로 저장한다.

| 저장소 | 용도 | 누적 방식 |
|---|---|---|
| `exports/*_latest.json` | 운영 백업·디버깅용 최신 결과 | 실행마다 같은 파일을 원자적으로 교체 |
| PostgreSQL `pipeline_runs`, `ranking_snapshots` | 실행 상태와 실행별 고정 결과 API | 실행마다 누적 |
| `runtime-data/ranker-history.sqlite3` | 독립 랭커의 직전 순위·점수 비교 호환 | 실행마다 누적되며 필요할 때 수동 정리 |

`exports`의 JSON은 파일 수가 늘어나지 않는다. PostgreSQL과 레거시 SQLite 이력만 보존 정책의 대상이다.

## 기본 보존 정책

```dotenv
HISTORY_CLEANUP_ENABLED=true
RUN_RETENTION_DAYS=90
FAILED_RUN_RETENTION_DAYS=14
MIN_SUCCESSFUL_RUNS_TO_KEEP=10
```

- 성공한 실행과 `ranking_snapshots`: 90일 보존
- 실패·취소된 실행: 14일 보존
- 날짜와 관계없이 최근 성공 실행 10개는 항상 보존
- `PipelineRun` 삭제 시 연결된 `RankingSnapshot`도 함께 삭제
- 레거시 SQLite도 성공 실행 90일, 최소 최근 10회 기준으로 정리 후 `VACUUM`
- `QUEUED`, `RUNNING` 실행은 자동 삭제하지 않는다

## 자동 실행

현재 자동 이력 정리는 비활성화되어 있다. Celery Beat 일정은 등록하지 않으며, 주기
업데이트와 보존 자동화 정책을 다시 설계하기 전까지 운영자가 필요할 때 수동 실행한다.

## 수동 실행

```bash
ai-service cleanup-history
```

응답 예시:

```json
{
  "enabled": true,
  "cutoffs": {
    "completed_before": "2026-05-25T00:00:00+00:00",
    "failed_before": "2026-08-09T00:00:00+00:00"
  },
  "postgres": {
    "deleted_runs": 12,
    "deleted_snapshots": 1200,
    "protected_successful_runs": 10
  },
  "legacy_sqlite": {
    "enabled": true,
    "deleted_runs": 12,
    "remaining_runs": 10,
    "vacuumed": true
  }
}
```

## 운영 판단

현재 레거시 SQLite는 `rank_change`, `score_change` 계산과 독립 CLI 호환을 위해 유지한다. 다만 무기한 증가하지 않도록 PostgreSQL과 같은 성공 이력 보존 기간을 적용한다.

장기적으로는 직전 성공 스냅샷 조회를 PostgreSQL로 완전히 이전한 뒤 `ranker-history.sqlite3`를 제거할 수 있다. 그 전까지는 이중 저장을 의도된 호환 계층으로 취급한다.

## 백업

정리 작업은 운영 DB 백업을 대체하지 않는다. 배포 환경에서는 다음을 별도로 구성한다.

- PostgreSQL 정기 백업
- 최소 한 개의 최근 성공 랭킹을 메인 백엔드에 캐시
- 새 분석 실패 시 직전 성공 결과 유지
- 보존 기간 변경 전 스토리지 사용량과 복구 요구사항 검토

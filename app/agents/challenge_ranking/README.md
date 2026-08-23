# Challenge Ranking Agent

Instagram Reels 스타일의 국내 챌린지를 자동 발견하고 Top 100을 산출한다.

공개 Agent 경계:

- `service.py`: 작업 생성, 파이프라인 실행, 실행별 결과 조회
- `app/api/v1/ranking_runs.py`: 비동기 실행 API
- `app/api/v1/challenges.py`: 현재 유효 랭킹 API

현재 분석 엔진은 `app/ranker_core`에 있다. API와 worker는 가능한 한 이 Agent의 `service.py` 경계를 통해 접근한다.

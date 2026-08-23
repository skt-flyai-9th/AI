# Agents

이 디렉터리는 독립 AI 서버가 제공하는 기능 단위 경계다.

현재 구현된 Agent:

- `challenge-ranking`: 국내 유행 챌린지 Top 100과 대표·가이드 영상 제공

다른 Agent는 아직 구현되어 있지 않다. 새 Agent는 실제 API 계약과 실행 모델이 확정된 뒤 추가한다.

등록 정보는 `app/agents/registry.py`, 확장 규칙은 `docs/ADDING_AN_AGENT.md`를 참고한다.

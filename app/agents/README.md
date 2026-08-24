# Agents

이 디렉터리는 독립 AI 서버가 제공하는 기능 단위 경계다.

현재 구현된 Agent:

- `challenge-ranking`: 국내 유행 챌린지 Top 100과 대표·가이드 영상 제공
- `shortform`: 대화로 프로젝트 brief를 확정하고 호환 영상편집DB 버전 1개 추천
- `editing`: 촬영 영상 분석, EditRecipe 검증·수정, REALS Renderer 실행

`Database Knowledge Manager`는 Agent와 별도의 운영 컴포넌트로, 상권분석DB와
영상편집DB의 후보·검증·승인·활성화 수명주기를 관리한다. 새 Agent는 실제 API 계약과
실행 모델이 확정된 뒤 추가한다.

등록 정보는 `app/agents/registry.py`, 확장 규칙은 `docs/ADDING_AN_AGENT.md`를 참고한다.

# WorkLog — 작업 진행 내역

> 최신 항목이 위. 오류와 수정 내역 포함.

## 2026-07-14 — 프로젝트 초기화 (M0)

### 진행한 작업
- 프로젝트 방향 확정: Claude API 직접 구현 / Python / 자율 협업(메시지 패싱) /
  범용 태스크 / 로컬 서버 + 웹 대시보드. 사용자 질의응답으로 결정 (DecisionLog D-001~D-005).
- 기술 조사 수행 (Research.md): Anthropic 스택 4가지 구축 방식 비교, Opus 4.8 기준
  API 변경사항(adaptive thinking, 샘플링 파라미터 제거, 프리필 불가), 프롬프트 캐싱 전략,
  멀티 에이전트 패턴별 위험(자율 협업의 수렴 실패 문제) 정리.
- 필수 문서 세트 생성: ProjectContext / DecisionLog / Plan / IA / UserScenarios /
  Personas / Process / ReviewChecklist / Research / WorkLog.
- 구현 계획 수립 (Plan.md): M1 계약 → M2 코어 엔진 → M3 서버 → M4 대시보드 → M5 견고화.

### 오류/이슈
- 없음 (문서 작업만 수행).

### 다음 할 일
- 미결 사항 확인: 패키지 이름(`agora` 제안), 기본 팀 구성 역할, 에이전트 도구 범위 (Plan.md 미결 사항 참조)
- git 저장소 초기화 여부 결정 (현재 git repo 아님)
- M1(계약 확정) 착수: `feat/m1-contracts` 브랜치

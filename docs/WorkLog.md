# WorkLog — 작업 진행 내역

> 최신 항목이 위. 오류와 수정 내역 포함.

## 2026-07-14 — 설계 갭 검토 및 코어 의미론 확정 (M1 준비)

### 진행한 작업
- 전체 설계 문서 검토로 M1 착수 전 설계 갭 식별: 인박스 소비 정책, 대화 이력 표현,
  세션 상태 기계(idle의 성공/실패 분류 모호), submit_result 경합, idle 판정 레이스,
  대시보드 접근 범위, 동시 세션 정책.
- 사용자 정책 결정 3건: **화백 합의 모드**(D-011 — submit_result 후 투표 승인,
  기본 만장일치), **localhost 전용**(D-012 — P-02 LAN 시나리오는 확장으로 이연),
  **동시 세션 1개**(D-013).
- 기술 설계 4건을 Plan.md "코어 의미론" 절로 확정: 인박스 배치 소비(메시지 1건당
  1호출 금지), 발신자 태깅 user 턴 병합 + 이력 상한 절단, 세션 상태 기계
  (voting 상태·fail_reason enum·agent_error 시 dead 처리), 단일 감시 태스크 idle 판정.
- 문서 정합화: Plan(M1 계약에 vote/approval/fail_reason 반영, M2 실패 경로 테스트에
  합의 경로 추가), IA(SC-01 제출 비활성, SC-03 투표 표시), UserScenarios(US-01 갱신,
  US-06 반려/재제출 추가), README, ProjectContext.

### 오류/이슈
- 없음 (문서 작업만 수행).

### 다음 할 일
- M1(계약 확정) 착수: `feat/m1-contracts` 브랜치 — contracts.py + llm/base.py +
  팀 YAML 스키마 + SSE 이벤트 계약
- M2 착수 전 스파이크: ChatGPT subscription(OAuth) 연동 검증, GPT-5.6 모델 ID 확인

## 2026-07-14 — 패키지 이름·기본 모델 결정 (M0 후속)

### 진행한 작업
- 패키지 이름 확정: `hwabaek` (D-010). 후보 약 50개를 PyPI 등록 여부로 스크리닝한 뒤
  (미등록: hwabaek/thinktank/convene/warroom/moot/dure/watercooler/jamsession 등)
  사용자 선택으로 확정. Plan/README의 작업명 `agora`(PyPI 등록됨) 교체.
- 기본 모델 변경: `claude-opus-4-8` → OpenAI **GPT-5.6 Terra** (D-008, D-007 번복).
  사용자 결정(ChatGPT subscription 연동 전제). 웹 조사로 사실 확인 — GPT-5.6은
  2026-07-09 출시 3티어(Sol/Terra/Luna), 구독과 API 과금은 분리이나
  "Sign in with ChatGPT"(BYOS OAuth) 경로 존재. Research.md §6에 기록.
- LLM 계층 멀티 프로바이더 추상화 결정 (D-009, D-001 일부 수정) — Plan의 M1/M2와
  디렉터리 구조(`llm/` 서브패키지: base 계약 + openai/anthropic 어댑터) 갱신.
- 문서 정합화: DecisionLog(D-008~D-010), ProjectContext, Plan, README, Research, Personas.

### 오류/이슈
- openai.com 공식 문서가 자동화 접근을 403으로 차단 — GPT-5.6의 정확한 API 모델 ID
  미확인(`gpt-5.6-terra` 추정). Plan 미결 사항으로 등재.

### 다음 할 일
- M2 착수 전 스파이크: ChatGPT subscription(OAuth) 연동 실현 가능성 검증
- GPT-5.6 정확한 API 모델 ID 확인
- M1(계약 확정) 착수: `feat/m1-contracts` 브랜치 — `llm/base.py` LLM 클라이언트 계약 포함

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

"""ConsensusEngine — 제안·투표·정족수 판정 (Plan 코어 의미론 §5, D-016/018/020/021).

이 파일은 M2a 인터페이스 확정본이다. 구현 규칙:

- **판정만 반환한다** — 세션 상태 전환은 SessionManager의 책임 (D-021).
  이 모듈은 세션·버스·LLM을 알지 못하고 contracts 타입만 다룬다.
- **세션당 활성(pending) 제안 최대 1개** — 활성 제안이 있는 동안 open_proposal은
  도메인 오류. version은 1부터 단조 증가하며, 반려된 제안은 다음 제안 생성 시
  SUPERSEDED로 전환된다.
- **투표 검증의 단일 지점** — contracts.validate_vote(제안 수준) +
  VoteTally.with_vote(심의자 자격·중복)를 사용하고 검증 로직을 중복하지 않는다.
  이전 제안에 대한 늦은 투표는 ContractError가 아니라 **무시(None 반환)**로
  처리한다 — 에이전트 도구 오류로 되돌릴 가치가 없는 정상 경합이다.
- **심의자 스냅샷 불변** (D-018): open_proposal 시점의 생존 에이전트(제출자
  제외)로 확정하고 이후 변경하지 않는다.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from hwabaek.contracts import (
    ApprovalConfig,
    ContractError,
    ProposalOutcome,
    ProposalStatus,
    ResultProposal,
    Vote,
    VoteDecision,
    VoteTally,
)


class ConsensusError(ContractError):
    """합의 규칙 위반 — voting 중 중복 제안 등. 도구 오류로 에이전트에게 반환된다."""


@dataclass(frozen=True)
class ConsensusState:
    """현재 심의 상태 스냅샷 — SessionManager가 이벤트 발행에 사용."""

    proposal: ResultProposal
    tally: VoteTally
    outcome: ProposalOutcome


class ConsensusEngine:
    """세션 1개의 합의 상태를 관리한다. 모든 메서드는 동기(순수 상태 조작)다."""

    def __init__(
        self,
        session_id: str,
        approval: ApprovalConfig,
        *,
        clock: Callable[[], str],
        id_factory: Callable[[], str],
    ) -> None:
        raise NotImplementedError

    @property
    def active(self) -> ConsensusState | None:
        """활성(pending) 제안의 현재 상태. 없으면 None."""
        raise NotImplementedError

    def open_proposal(
        self, proposer: str, content: str, alive_agents: frozenset[str]
    ) -> ConsensusState:
        """새 제안을 연다 — running 상태에서 submit_result 처리.

        - 활성 제안이 있으면 ConsensusError (voting 중 중복 submit 거부).
        - 직전 반려 제안이 있으면 SUPERSEDED로 전환하고 version을 잇는다.
        - 심의자 스냅샷 = alive_agents - {proposer} (D-018).
        - 반환된 outcome이 즉시 APPROVED일 수 있다 (first 모드).
        """
        raise NotImplementedError

    def register_vote(
        self, voter: str, decision: VoteDecision, reason: str
    ) -> tuple[Vote, ConsensusState] | None:
        """투표를 등록하고 갱신된 판정을 반환한다.

        - 활성 제안이 없거나 늦은 투표(활성 제안 불일치)는 **None 반환(무시)**.
        - 자기 투표·비심의자·중복 투표는 ContractError — 에이전트 도구 오류로 반환.
        - Vote 레코드(id/created_at 부여)를 함께 반환한다 — 영속화·이벤트용.
        """
        raise NotImplementedError

    def expire_pending(self) -> ConsensusState | None:
        """voting_timeout 만료 처리 — 미응답 전원을 기권 처리하고 판정을 반환한다.

        활성 제안이 없으면 None. 반환된 outcome은 PENDING일 수 없다
        (기권 처리 후에는 모든 정책이 확정 판정을 낸다).
        """
        raise NotImplementedError

    def resolve(self, outcome: ProposalOutcome) -> ResultProposal:
        """판정 확정을 제안 상태에 반영한다 — APPROVED/REJECTED만 허용.

        SessionManager가 상태 전환 직전에 호출한다. 반환값은 갱신된 제안
        (영속화용). NO_QUORUM은 제안을 REJECTED로 마감한다(세션은 실패).
        """
        raise NotImplementedError

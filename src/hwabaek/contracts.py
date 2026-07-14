"""hwabaek 핵심 계약 — 메시지/에이전트/팀/세션 스키마와 상태 기계.

M1에서 확정하는 계약 모듈. 모든 상위 모듈(bus/agent/session/server)은 이 타입 위에서
구현한다 (docs/Plan.md "코어 의미론", DecisionLog D-011~D-013 참조).

규칙:
- 표준 라이브러리 외 의존성 금지.
- 모든 데이터클래스는 불변(frozen). 상태 변화는 새 인스턴스를 반환하는 메서드로 표현.
- 이 모듈은 시계(clock)를 읽지 않는다 — 타임스탬프는 호출자(버스/세션 엔진)가 찍어서
  전달한다. 결정적 테스트를 위한 설계.
- 검증 실패는 ContractError로 통일하며, 오류 메시지는 영어 ASCII로 작성한다.
"""
from __future__ import annotations

import enum
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any

# 정확한 API 모델 ID는 미확정(추정치) — M2 스파이크에서 확정 후 이 상수만 갱신한다 (D-008).
DEFAULT_MODEL = "gpt-5.6-terra"

# recipients에 쓰는 브로드캐스트 지정자. 단독으로만 사용한다.
BROADCAST = "*"

# 에이전트 이름 규칙: 소문자 시작, 소문자/숫자/밑줄/하이픈, 최대 32자.
AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class ContractError(ValueError):
    """계약 위반 (스키마 검증 실패)."""


class InvalidTransition(ContractError):
    """세션 상태 기계가 허용하지 않는 전이."""


# ---------------------------------------------------------------------------
# 열거형
# ---------------------------------------------------------------------------

class MessageType(str, enum.Enum):
    CHAT = "chat"                        # 일반 협업 메시지
    RESULT_PROPOSAL = "result_proposal"  # 최종 결과 초안 제출 (submit_result)
    VOTE = "vote"                        # 초안에 대한 투표 (vote_result)


class VoteDecision(str, enum.Enum):
    APPROVE = "approve"
    REJECT = "reject"


class SessionStatus(str, enum.Enum):
    RUNNING = "running"
    VOTING = "voting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# 종료 상태 — 이후 어떤 전이도 불가.
TERMINAL_STATUSES = frozenset(
    {SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED}
)


class FailReason(str, enum.Enum):
    BUDGET = "budget"            # 토큰 예산 초과
    MESSAGES = "messages"        # 메시지 수 상한 초과
    IDLE = "idle"                # 전원 유휴 — 결과물 없이 종료
    AGENT_ERROR = "agent_error"  # 생존 에이전트 부족 (오류로 dead 처리 누적)
    NO_QUORUM = "no_quorum"      # 합의 정족수 미달 (전원 기권 등)


class ApprovalPolicy(str, enum.Enum):
    """합의 정족수 정책 (D-016). voters는 항상 '생존한 심의 대상자(제출자 제외)'.

    - UNANIMOUS: 생존 심의자 전원이 approve해야 확정 (기본 — 엄밀한 화백).
      미투표(만료 기권)는 승인으로 간주하지 않는다 → 기권 존재 시 no_quorum.
    - MAJORITY: 생존 심의자 '전체'의 과반이 approve해야 확정 (유효 투표 과반 아님).
    - PARTICIPATING_UNANIMOUS: 실제 유효 투표를 한 에이전트 전원이 approve하면 확정
      (기권 제외 판정 — 구 D-011의 unanimous 규칙이 이 정책으로 이동).
    - FIRST: 투표 생략, 첫 제출 즉시 확정.
    """

    UNANIMOUS = "unanimous"
    MAJORITY = "majority"
    PARTICIPATING_UNANIMOUS = "participating_unanimous"
    FIRST = "first"


class AgentState(str, enum.Enum):
    IDLE = "idle"          # 인박스 대기 중
    THINKING = "thinking"  # LLM 호출 중
    VOTING = "voting"      # 투표 대기 중 (voting 세션에서 아직 미투표)
    DEAD = "dead"          # 재시도 소진 후 오류로 제외됨


class ProposalOutcome(str, enum.Enum):
    PENDING = "pending"      # 투표 진행 중
    APPROVED = "approved"    # 확정 → 세션 completed
    REJECTED = "rejected"    # 반려 → 세션 running 복귀
    NO_QUORUM = "no_quorum"  # 유효 투표 없음 → 세션 failed(no_quorum)


class EventType(str, enum.Enum):
    SESSION_STATUS = "session_status"  # 세션 상태 전이
    MESSAGE = "message"                # 버스에 실린 메시지
    AGENT_STATE = "agent_state"        # 에이전트 상태 변화
    USAGE = "usage"                    # 누적 사용량 갱신
    VOTE_STATUS = "vote_status"        # 투표 현황 변화
    RESULT = "result"                  # 확정된 최종 결과


# ---------------------------------------------------------------------------
# 사용량
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Usage:
    """프로바이더 중립 토큰 사용량. LLM 어댑터가 이 형태로 정규화한다 (D-009)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            value = getattr(self, name)
            # bool은 int의 서브클래스지만 사용량 값으로는 타입 오류다.
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractError(f"Usage.{name} must be a non-negative int, got {value!r}")

    def __add__(self, other: "Usage") -> "Usage":
        if not isinstance(other, Usage):
            return NotImplemented
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )

    @property
    def total_tokens(self) -> int:
        """예산(token_budget) 판정에 쓰는 합계 — 캐시 읽기/쓰기 포함 전체."""
        return (
            self.input_tokens + self.output_tokens
            + self.cache_read_tokens + self.cache_write_tokens
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Usage":
        return cls(**data)


# ---------------------------------------------------------------------------
# 메시지
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Message:
    """버스에 실리는 단일 메시지. created_at은 버스가 ISO 8601(UTC)로 찍는다.

    타입별 규칙 (Plan "코어 의미론" §5, D-016):
    - CHAT: vote/proposal_id 금지. 수신자는 특정 에이전트(들) 또는 브로드캐스트.
    - RESULT_PROPOSAL: 브로드캐스트 강제(전원 심의). content가 결과 초안이며
      proposal_id는 대응하는 ResultProposal.id (버전 추적).
    - VOTE: vote/proposal_id 필수, 브로드캐스트 강제(투표 공개 — 화백).
      content는 투표 사유(반려 사유 전달에 사용). 이전 제안에 대한 늦은 투표는
      엔진이 proposal_id 불일치로 무시한다.
    """

    id: str
    session_id: str
    sender: str
    recipients: tuple[str, ...]
    type: MessageType
    content: str
    created_at: str
    vote: VoteDecision | None = None
    proposal_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("id", "session_id", "sender", "created_at"):
            if not getattr(self, name):
                raise ContractError(f"Message.{name} must be non-empty")
        if not isinstance(self.recipients, tuple) or not self.recipients:
            raise ContractError("Message.recipients must be a non-empty tuple")
        if BROADCAST in self.recipients and self.recipients != (BROADCAST,):
            raise ContractError("broadcast recipient '*' must be used alone")
        if self.sender == BROADCAST:
            raise ContractError("Message.sender must not be the broadcast marker")
        if self.type is MessageType.CHAT:
            if not self.content:
                raise ContractError("chat message content must be non-empty")
            if self.vote is not None or self.proposal_id is not None:
                raise ContractError("chat message must not carry vote or proposal_id")
        elif self.type is MessageType.RESULT_PROPOSAL:
            if not self.content:
                raise ContractError("result proposal content must be non-empty")
            if self.recipients != (BROADCAST,):
                raise ContractError("result proposal must be broadcast to all agents")
            if self.vote is not None:
                raise ContractError("result proposal must not carry a vote")
            # 제안 버전 추적(D-016): 메시지는 자신이 실어 나르는 ResultProposal.id를 가리킨다.
            if not self.proposal_id:
                raise ContractError("result proposal requires proposal_id")
        elif self.type is MessageType.VOTE:
            if self.vote is None or not self.proposal_id:
                raise ContractError("vote message requires vote and proposal_id")
            if self.recipients != (BROADCAST,):
                raise ContractError("vote must be broadcast to all agents")

    @property
    def is_broadcast(self) -> bool:
        return self.recipients == (BROADCAST,)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["recipients"] = list(self.recipients)
        data["type"] = self.type.value
        data["vote"] = self.vote.value if self.vote is not None else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        payload = dict(data)
        payload["recipients"] = tuple(payload["recipients"])
        payload["type"] = MessageType(payload["type"])
        if payload.get("vote") is not None:
            payload["vote"] = VoteDecision(payload["vote"])
        return cls(**payload)


# ---------------------------------------------------------------------------
# 에이전트 / 팀 설정
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AgentSpec:
    """팀을 구성하는 에이전트 1개의 명세. 팀 설정 YAML에서 로드된다."""

    name: str
    role: str
    system_prompt: str
    model: str | None = None  # None이면 TeamConfig.default_model 사용
    max_turns: int = 50       # 에이전트당 LLM 호출 상한 (세션 예산과 별개의 방어선)

    def __post_init__(self) -> None:
        if not AGENT_NAME_RE.match(self.name):
            raise ContractError(
                f"agent name {self.name!r} is invalid: use lowercase letters, digits,"
                " '_' or '-', starting with a letter, at most 32 chars"
            )
        if not self.role:
            raise ContractError(f"agent {self.name!r}: role must be non-empty")
        if not self.system_prompt:
            raise ContractError(f"agent {self.name!r}: system_prompt must be non-empty")
        if self.model is not None and not self.model:
            raise ContractError(f"agent {self.name!r}: model must be non-empty or omitted")
        if (
            isinstance(self.max_turns, bool)
            or not isinstance(self.max_turns, int)
            or self.max_turns < 1
        ):
            raise ContractError(f"agent {self.name!r}: max_turns must be a positive int")


@dataclass(frozen=True)
class TerminationPolicy:
    """종료 정책 — 자율 협업의 수렴 안전장치 (Plan 종료 제어 + D-011)."""

    max_messages: int = 100
    token_budget: int = 200_000
    idle_timeout: float = 30.0
    approval: ApprovalPolicy = ApprovalPolicy.UNANIMOUS

    def __post_init__(self) -> None:
        for name in ("max_messages", "token_budget"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ContractError(f"termination.{name} must be a positive int")
        if (
            isinstance(self.idle_timeout, bool)
            or not isinstance(self.idle_timeout, (int, float))
            or self.idle_timeout <= 0
        ):
            raise ContractError("termination.idle_timeout must be a positive number")


@dataclass(frozen=True)
class TeamConfig:
    """팀 구성 전체 — 에이전트 목록과 종료 정책. YAML 1파일에 대응한다."""

    name: str
    agents: tuple[AgentSpec, ...]
    description: str = ""
    default_model: str = DEFAULT_MODEL
    termination: TerminationPolicy = field(default_factory=TerminationPolicy)

    def __post_init__(self) -> None:
        if not AGENT_NAME_RE.match(self.name):
            raise ContractError(
                f"team name {self.name!r} is invalid: use lowercase letters, digits,"
                " '_' or '-', starting with a letter, at most 32 chars"
            )
        if not self.default_model:
            raise ContractError("team default_model must be non-empty")
        if not isinstance(self.agents, tuple) or not self.agents:
            raise ContractError("team must define at least one agent")
        names = [agent.name for agent in self.agents]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ContractError(f"duplicate agent names in team: {', '.join(duplicates)}")

    def model_for(self, agent_name: str) -> str:
        """에이전트의 실효 모델 — 개별 지정이 없으면 팀 기본값."""
        for agent in self.agents:
            if agent.name == agent_name:
                return agent.model or self.default_model
        raise ContractError(f"unknown agent name: {agent_name!r}")


# ---------------------------------------------------------------------------
# 화백 합의 — 결과 제안과 투표 집계 (순수 함수, 세션 엔진이 재사용)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResultProposal:
    """결과 제안 1건의 도메인 레코드 (D-016).

    반려 후 재제출은 version을 올린 새 제안으로 표현한다 — 투표는 반드시
    proposal_id로 대상 제안을 가리키며, 이전 제안에 대한 늦은 투표는 현재
    제안에 반영하지 않는다(엔진 규칙). running 상태에서만 새 제안을 만들 수
    있고, voting 중 추가 제안은 도메인 오류다(엔진이 강제).
    """

    id: str
    session_id: str
    proposer: str
    version: int
    content: str
    created_at: str

    def __post_init__(self) -> None:
        for name in ("id", "session_id", "proposer", "content", "created_at"):
            if not getattr(self, name):
                raise ContractError(f"ResultProposal.{name} must be non-empty")
        if isinstance(self.version, bool) or not isinstance(self.version, int) \
                or self.version < 1:
            raise ContractError("ResultProposal.version must be a positive int")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResultProposal":
        return cls(**data)


@dataclass(frozen=True)
class VoteTally:
    """제안 1건에 대한 투표 현황. voters는 제안 생성 시점의 '생존' 심의 대상
    (제출자 제외) 전원이며, 심의 중 사망한 에이전트는 with_voter_removed로
    제외해 정족수를 재계산한다 (D-016).

    기권(abstained)은 투표 시간 만료까지의 무투표를 엔진이 기권 처리한 것 —
    어떤 정책에서도 기권을 암묵적 승인으로 간주하지 않는다.
    """

    voters: frozenset[str]
    approvals: frozenset[str] = frozenset()
    rejections: frozenset[str] = frozenset()
    abstained: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        groups = (self.approvals, self.rejections, self.abstained)
        combined: set[str] = set()
        total = 0
        for group in groups:
            if not group <= self.voters:
                raise ContractError("vote tally contains a non-voter")
            combined |= group
            total += len(group)
        if len(combined) != total:
            raise ContractError("an agent appears in more than one vote group")

    def with_vote(self, agent: str, decision: VoteDecision) -> "VoteTally":
        """투표 1건 반영 — 이미 응답한 에이전트의 재투표는 계약 위반."""
        if agent not in self.voters:
            raise ContractError(f"agent {agent!r} is not a voter for this proposal")
        if agent in self.approvals | self.rejections | self.abstained:
            raise ContractError(f"agent {agent!r} has already responded")
        if decision is VoteDecision.APPROVE:
            return replace(self, approvals=self.approvals | {agent})
        return replace(self, rejections=self.rejections | {agent})

    def with_abstained(self, agents: frozenset[str]) -> "VoteTally":
        """무응답 에이전트들을 기권 처리 (이미 응답한 에이전트는 무시)."""
        responded = self.approvals | self.rejections | self.abstained
        return replace(self, abstained=self.abstained | (agents & self.voters - responded))

    def with_voter_removed(self, agent: str) -> "VoteTally":
        """심의 중 사망한 에이전트를 심의 대상에서 제외한다 — 정족수 재계산 (D-016).

        모든 응답 그룹에서도 함께 제거한다(사망자의 기존 투표는 무효).
        """
        if agent not in self.voters:
            raise ContractError(f"agent {agent!r} is not a voter for this proposal")
        removed = frozenset({agent})
        return VoteTally(
            voters=self.voters - removed,
            approvals=self.approvals - removed,
            rejections=self.rejections - removed,
            abstained=self.abstained - removed,
        )

    @property
    def pending(self) -> frozenset[str]:
        return self.voters - self.approvals - self.rejections - self.abstained

    def decide(self, policy: ApprovalPolicy) -> ProposalOutcome:
        """정족수 판정 (D-016). 결과가 이미 확정 가능하면 PENDING을 기다리지 않는다.

        공통 규칙:
        - FIRST: 심의 생략, 즉시 확정.
        - voters가 비면(1인 팀 / 심의자 전원 사망) 심의자가 없으므로 즉시 확정.
        - 기권은 어떤 정책에서도 암묵적 승인이 아니다.

        정책별 규칙:
        - UNANIMOUS: 반대 1표면 즉시 반려. 생존 심의자 '전원' approve여야 확정 —
          기권(만료 미투표)이 하나라도 있으면 확정 불가 → 전원 응답 시 NO_QUORUM.
        - MAJORITY: 생존 심의자 '전체'의 과반 approve로 확정(조기 확정 가능).
          남은 미응답을 전부 approve로 가정해도 과반이 불가능해지면 조기 종료 —
          반대표가 있으면 REJECTED, 기권만으로 불가능해졌으면 NO_QUORUM.
        - PARTICIPATING_UNANIMOUS: 반대 1표면 즉시 반려. 전원 응답 후 유효 투표
          (approve/reject)를 한 전원이 approve면 확정, 유효 투표가 없으면 NO_QUORUM.
        """
        if policy is ApprovalPolicy.FIRST or not self.voters:
            return ProposalOutcome.APPROVED

        n = len(self.voters)
        all_responded = not self.pending

        if policy is ApprovalPolicy.UNANIMOUS:
            if self.rejections:
                return ProposalOutcome.REJECTED
            if self.approvals == self.voters:
                return ProposalOutcome.APPROVED
            if all_responded:
                # 반대는 없지만 기권이 존재 — 전원 승인 실패.
                return ProposalOutcome.NO_QUORUM
            return ProposalOutcome.PENDING

        if policy is ApprovalPolicy.MAJORITY:
            if len(self.approvals) * 2 > n:
                return ProposalOutcome.APPROVED
            # 남은 미응답이 전부 approve해도 과반 불가 → 조기 종료.
            if (len(self.approvals) + len(self.pending)) * 2 <= n:
                return (
                    ProposalOutcome.REJECTED if self.rejections
                    else ProposalOutcome.NO_QUORUM
                )
            return ProposalOutcome.PENDING

        # PARTICIPATING_UNANIMOUS
        if self.rejections:
            return ProposalOutcome.REJECTED
        if all_responded:
            return (
                ProposalOutcome.APPROVED if self.approvals
                else ProposalOutcome.NO_QUORUM
            )
        return ProposalOutcome.PENDING


# ---------------------------------------------------------------------------
# 세션 — 상태 기계
# ---------------------------------------------------------------------------

_ALLOWED_TRANSITIONS: dict[SessionStatus, frozenset[SessionStatus]] = {
    SessionStatus.RUNNING: frozenset(
        {SessionStatus.VOTING, SessionStatus.FAILED, SessionStatus.CANCELLED}
    ),
    SessionStatus.VOTING: frozenset(
        {
            SessionStatus.RUNNING,    # 반려 → 논의 재개
            SessionStatus.COMPLETED,  # 정족수 승인
            SessionStatus.FAILED,
            SessionStatus.CANCELLED,
        }
    ),
    SessionStatus.COMPLETED: frozenset(),
    SessionStatus.FAILED: frozenset(),
    SessionStatus.CANCELLED: frozenset(),
}


@dataclass(frozen=True)
class Session:
    """태스크 1건의 수명주기. 상태 전이는 with_status()로만 수행한다.

    불변식:
    - FAILED이면 fail_reason 필수, 그 외 상태에서는 금지.
    - fail_detail(귀책 등 실패 상세 — 오류 귀책 원칙)은 FAILED에서만 허용.
    - COMPLETED이면 result/submitted_by 필수.
    - 종료 상태(TERMINAL_STATUSES)이면 finished_at 필수, 그 외에는 금지.
    """

    id: str
    task: str
    team_name: str
    created_at: str
    status: SessionStatus = SessionStatus.RUNNING
    result: str | None = None
    submitted_by: str | None = None
    fail_reason: FailReason | None = None
    fail_detail: str | None = None
    usage: Usage = field(default_factory=Usage)
    finished_at: str | None = None

    def __post_init__(self) -> None:
        for name in ("id", "task", "team_name", "created_at"):
            if not getattr(self, name):
                raise ContractError(f"Session.{name} must be non-empty")
        if (self.status is SessionStatus.FAILED) != (self.fail_reason is not None):
            raise ContractError("fail_reason is required iff status is failed")
        if self.fail_detail is not None and self.status is not SessionStatus.FAILED:
            raise ContractError("fail_detail is only allowed when status is failed")
        if self.status is SessionStatus.COMPLETED and (
            self.result is None or self.submitted_by is None
        ):
            raise ContractError("completed session requires result and submitted_by")
        if (self.status in TERMINAL_STATUSES) != (self.finished_at is not None):
            raise ContractError("finished_at is required iff status is terminal")

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def with_usage(self, delta: Usage) -> "Session":
        """사용량 누적 — 종료 후에는 추가 사용이 없어야 한다 (취소 후 호출 금지 원칙)."""
        if self.is_terminal:
            raise ContractError("cannot add usage to a terminal session")
        return replace(self, usage=self.usage + delta)

    def with_status(
        self,
        new_status: SessionStatus,
        *,
        fail_reason: FailReason | None = None,
        fail_detail: str | None = None,
        result: str | None = None,
        submitted_by: str | None = None,
        finished_at: str | None = None,
    ) -> "Session":
        """상태 전이. 허용 전이표와 불변식을 강제하고 새 Session을 반환한다.

        종료 전이의 finished_at은 호출자(엔진)가 찍어서 전달한다.
        FAILED 전이의 fail_detail에는 귀책 구분(클라이언트 잘못 vs 프로바이더 혼잡)을
        포함한 실패 상세를 영어 ASCII로 남긴다.
        """
        if new_status not in _ALLOWED_TRANSITIONS[self.status]:
            raise InvalidTransition(
                f"cannot transition from {self.status.value} to {new_status.value}"
            )
        return replace(
            self,
            status=new_status,
            fail_reason=fail_reason,
            fail_detail=fail_detail,
            result=result if result is not None else (
                None if new_status is SessionStatus.RUNNING else self.result
            ),
            submitted_by=submitted_by if submitted_by is not None else (
                None if new_status is SessionStatus.RUNNING else self.submitted_by
            ),
            finished_at=finished_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task": self.task,
            "team_name": self.team_name,
            "created_at": self.created_at,
            "status": self.status.value,
            "result": self.result,
            "submitted_by": self.submitted_by,
            "fail_reason": self.fail_reason.value if self.fail_reason else None,
            "fail_detail": self.fail_detail,
            "usage": self.usage.to_dict(),
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        payload = dict(data)
        payload["status"] = SessionStatus(payload["status"])
        if payload.get("fail_reason") is not None:
            payload["fail_reason"] = FailReason(payload["fail_reason"])
        payload["usage"] = Usage.from_dict(payload["usage"])
        return cls(**payload)


# ---------------------------------------------------------------------------
# SSE 이벤트 계약 (스키마 상세는 docs/EventContract.md)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Event:
    """세션 이벤트 스트림의 단위. seq는 세션 내 단조 증가 — SSE 재구독 복원 기준.

    payload 스키마는 type별로 아래 make_*_event 헬퍼가 고정한다.
    """

    seq: int
    session_id: str
    type: EventType
    at: str
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        if isinstance(self.seq, bool) or not isinstance(self.seq, int) or self.seq < 0:
            raise ContractError("Event.seq must be a non-negative int")
        if not self.session_id or not self.at:
            raise ContractError("Event.session_id and Event.at must be non-empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "session_id": self.session_id,
            "type": self.type.value,
            "at": self.at,
            "payload": self.payload,
        }


def make_session_status_event(seq: int, session: Session, at: str) -> Event:
    return Event(
        seq=seq,
        session_id=session.id,
        type=EventType.SESSION_STATUS,
        at=at,
        payload={
            "status": session.status.value,
            "fail_reason": session.fail_reason.value if session.fail_reason else None,
            "fail_detail": session.fail_detail,
        },
    )


def make_message_event(seq: int, message: Message) -> Event:
    return Event(
        seq=seq,
        session_id=message.session_id,
        type=EventType.MESSAGE,
        at=message.created_at,
        payload=message.to_dict(),
    )


def make_agent_state_event(
    seq: int,
    session_id: str,
    agent: str,
    state: AgentState,
    at: str,
    detail: str | None = None,
) -> Event:
    """detail에는 상태 변화의 사유를 담는다 — 특히 DEAD 전이 시 귀책을 포함한
    실패 상세(영어 ASCII)를 남긴다 (오류 귀책 원칙)."""
    return Event(
        seq=seq,
        session_id=session_id,
        type=EventType.AGENT_STATE,
        at=at,
        payload={"agent": agent, "state": state.value, "detail": detail},
    )


def make_usage_event(
    seq: int,
    session_id: str,
    usage: Usage,
    token_budget: int,
    at: str,
    per_agent: dict[str, Usage] | None = None,
) -> Event:
    """usage는 세션 누적치, per_agent는 에이전트별 누적치 전체 맵.

    per_agent는 매 발행 시 전체 맵을 다시 싣는다 — 대시보드가 누적 상태를
    유지할 필요 없이 마지막 이벤트만으로 복원 가능 (IA SC-03 에이전트 패널).
    """
    return Event(
        seq=seq,
        session_id=session_id,
        type=EventType.USAGE,
        at=at,
        payload={
            "usage": usage.to_dict(),
            "token_budget": token_budget,
            "per_agent": {
                name: agent_usage.to_dict()
                for name, agent_usage in sorted((per_agent or {}).items())
            },
        },
    )


def make_vote_status_event(
    seq: int, session_id: str, proposal_id: str, tally: VoteTally, at: str
) -> Event:
    return Event(
        seq=seq,
        session_id=session_id,
        type=EventType.VOTE_STATUS,
        at=at,
        payload={
            "proposal_id": proposal_id,
            "approvals": sorted(tally.approvals),
            "rejections": sorted(tally.rejections),
            "abstained": sorted(tally.abstained),
            "pending": sorted(tally.pending),
        },
    )


def make_result_event(seq: int, session: Session, at: str) -> Event:
    if session.status is not SessionStatus.COMPLETED:
        raise ContractError("result event requires a completed session")
    return Event(
        seq=seq,
        session_id=session.id,
        type=EventType.RESULT,
        at=at,
        payload={"result": session.result, "submitted_by": session.submitted_by},
    )

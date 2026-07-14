"""메시지 버스 — 에이전트별 인박스와 배달 규칙 (Plan 코어 의미론 §1, D-023).

이 파일은 M2a 인터페이스 확정본이다. 구현 규칙:

- **id/created_at/sequence 부여는 버스의 책임** — 계약(contracts)은 시계를 읽지
  않으므로 clock/id_factory를 주입받는다. sequence는 세션 단위 단조 증가
  (메시지만 카운트 — Event.sequence와 독립).
- **배달**: 특정 수신자는 해당 인박스에만, 브로드캐스트(`*`)는 발신자를 제외한
  전원의 인박스에 배달. 동일 message id의 중복 배달은 무시(멱등).
- **원자적 drain**: drain()은 호출 시점까지 쌓인 메시지 전부를 한 번에 비워
  sequence 오름차순으로 반환하고, 그 이후 도착분은 다음 배치로 넘긴다.
  asyncio 단일 이벤트 루프에서 await 없이 완결되어야 원자성이 보장된다.
- **관측 훅**: 배달된 모든 메시지는 on_message 콜백으로 통지된다 —
  SessionManager가 message 이벤트 발행·영속화·max_messages 판정에 사용.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable

from hwabaek.contracts import (
    BROADCAST,
    ContractError,
    Message,
    MessageType,
    VoteDecision,
)


class MessageBus:
    """세션 1개의 메시지 버스. 에이전트 이름 목록은 생성 시 고정된다."""

    def __init__(
        self,
        session_id: str,
        agent_names: tuple[str, ...],
        *,
        clock: Callable[[], str],
        id_factory: Callable[[], str],
        on_message: Callable[[Message], None] | None = None,
    ) -> None:
        """clock은 ISO 8601 UTC 문자열을, id_factory는 유일 id를 반환해야 한다."""
        raise NotImplementedError

    def post(
        self,
        *,
        sender: str,
        recipients: tuple[str, ...],
        type: MessageType,
        content: str,
        vote: VoteDecision | None = None,
        proposal_id: str | None = None,
    ) -> Message:
        """메시지를 생성(id/created_at/sequence 부여)·검증·배달하고 반환한다.

        검증은 Message 계약이 수행한다(자기송신 금지 포함). 추가로 버스는
        미등록 수신자를 ContractError로 거부한다. 배달 후 on_message 통지.
        """
        raise NotImplementedError

    async def wait_for_messages(self, agent_name: str) -> None:
        """해당 인박스에 메시지가 생길 때까지 대기한다 (이미 있으면 즉시 반환).

        취소(asyncio.CancelledError)는 그대로 전파한다 — 세션 종료 시
        SessionManager가 대기 중인 에이전트 태스크를 취소한다.
        """
        raise NotImplementedError

    def drain(self, agent_name: str) -> list[Message]:
        """인박스를 원자적으로 비워 sequence 오름차순 배치로 반환한다."""
        raise NotImplementedError

    def pending_count(self, agent_name: str) -> int:
        """인박스에 대기 중인 메시지 수 — idle 판정 재료."""
        raise NotImplementedError

    def total_posted(self) -> int:
        """세션에서 지금까지 발행된 메시지 총수 — max_messages 판정 재료."""
        raise NotImplementedError

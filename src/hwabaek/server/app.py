"""FastAPI 앱 조립 (M3).

create_app은 store·팀 소스·LLM 팩토리 주입을 받아 앱을 만든다 — 테스트가 실
OpenAI 클라이언트를 만들지 않도록 llm_factory_provider를 주입할 수 있게 한다.
lifespan에서 이전 running/voting 세션을 failed(interrupted)로 일괄 처리한다 (D-021).

시계·id 팩토리는 기본값(UTC ISO clock, 세션별 prefix+counter id)을 쓰되 주입 가능하다.
"""
from __future__ import annotations

import itertools
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from fastapi import FastAPI

from hwabaek.contracts import TeamConfig
from hwabaek.server.api import router
from hwabaek.server.events import (
    LLMFactoryProvider,
    SessionRegistry,
    mark_interrupted_sessions,
)

if TYPE_CHECKING:
    from hwabaek.store.base import Store

logger = logging.getLogger(__name__)


def default_clock() -> str:
    """UTC ISO 8601 타임스탬프."""
    return datetime.now(timezone.utc).isoformat()


def make_id_factory() -> Callable[[], str]:
    """세션 1개 전용 id 팩토리 — 세션별 uuid prefix + 단조 카운터.

    세션마다 새 prefix를 뽑으므로 event_id는 서버 전역에서 유일하다.
    """
    counter = itertools.count()
    prefix = uuid4().hex[:8]

    def factory() -> str:
        return f"{prefix}-{next(counter):06d}"

    return factory


def create_app(
    *,
    store: "Store | None" = None,
    teams_dir: str | Path = "configs",
    llm_factory_provider: LLMFactoryProvider,
    team_override: TeamConfig | None = None,
    clock: Callable[[], str] = default_clock,
    id_factory_provider: Callable[[], Callable[[], str]] = make_id_factory,
    default_team: str = "default",
    interrupt_on_startup: bool = True,
) -> FastAPI:
    """M3 서버 앱을 조립한다.

    store가 None이면 영속화 없이 활성 세션만 관측하는 데모 모드로 동작한다.
    team_override를 주면 모든 세션이 그 팀을 쓰고 GET /teams도 그 팀만 보여준다
    (밀폐 데모/테스트용).
    """
    registry = SessionRegistry(
        store=store,
        teams_dir=teams_dir,
        llm_factory_provider=llm_factory_provider,
        clock=clock,
        id_factory_provider=id_factory_provider,
        team_override=team_override,
        default_team=default_team,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 시작: 이전 running/voting 세션을 failed(interrupted)로 정리한다 (D-021).
        if interrupt_on_startup and store is not None:
            try:
                n = await mark_interrupted_sessions(store, clock)
                if n:
                    logger.info("marked %d interrupted session(s) as failed", n)
            except Exception:
                logger.exception("failed to mark interrupted sessions on startup")
        try:
            yield
        finally:
            # 종료: 활성 세션은 사용자 취소와 구분해 interrupted로 종결한다.
            active = registry.get_runner_for_shutdown()
            if active is not None and not active.done:
                active.manager.interrupt()
                await active.wait_done(timeout=5.0)
            if store is not None:
                await store.close()

    app = FastAPI(title="hwabaek", version="0.1.0", lifespan=lifespan)
    app.state.registry = registry
    app.include_router(router)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app

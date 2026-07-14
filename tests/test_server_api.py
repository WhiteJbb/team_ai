"""M3 서버 REST API 통합 테스트 — fastapi TestClient로 인프로세스 검증.

밀폐 원칙: 실키/실네트워크 금지. LLM은 스크립트/블로킹 Fake를 create_app의
llm_factory_provider로 주입한다 — 서버가 실 OpenAI 클라이언트를 만들지 않는다.
실패 경로(동시 세션 409 / 없는 세션 404 / 종료 세션 cancel 409 / 잘못된 body 422 /
interrupted 처리)를 명시적으로 검증한다.

데이터는 영어 ASCII, 주석은 한국어.
"""
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from hwabaek.contracts import (
    AgentSpec,
    ApprovalConfig,
    ApprovalPolicy,
    Session,
    SessionStatus,
    TeamConfig,
    TerminationPolicy,
)
from hwabaek.llm.fake import text_response, tool_response
from hwabaek.server import create_app

CLOCK = "2026-07-14T00:00:00Z"


# ---------------------------------------------------------------------------
# 테스트용 LLM 대역
# ---------------------------------------------------------------------------

class SoftScriptedLLM:
    """스크립트를 순서대로 소비하고, 소진 후에는 조용히 idle 텍스트를 돌려주는 대역.

    통합에서는 종료 뒤 잔여 브로드캐스트로 깨어난 에이전트가 한 번 더 호출을 시도할
    수 있어, 하드 소진(AssertionError) 대신 소프트 idle이 안정적이다.
    """

    def __init__(self, script) -> None:
        self._script = list(script)
        self._i = 0
        self.calls = []

    async def complete(self, request):
        self.calls.append(request)
        if self._i < len(self._script):
            item = self._script[self._i]
            self._i += 1
            if isinstance(item, BaseException):
                raise item
            return item
        return text_response("(idle)")


class BlockingLLM:
    """첫 호출부터 영원히 대기 — 세션을 running 상태로 붙잡아 둔다(취소로만 풀림)."""

    def __init__(self) -> None:
        self.calls = []

    async def complete(self, request):
        self.calls.append(request)
        await asyncio.Event().wait()  # 절대 set되지 않음 -> 태스크 취소 시 해제
        return text_response("unreachable")


# ---------------------------------------------------------------------------
# 팀/앱 조립 헬퍼
# ---------------------------------------------------------------------------

def _first_team() -> TeamConfig:
    """1인 first-mode 팀 — submit 1회로 즉시 completed(투표 없음)."""
    return TeamConfig(
        name="demo",
        agents=(
            AgentSpec(name="solo", role="tester", system_prompt="You are a test agent."),
        ),
        termination=TerminationPolicy(
            max_messages=20,
            token_budget=100_000,
            idle_timeout=5.0,
            approval=ApprovalConfig(mode=ApprovalPolicy.FIRST, voting_timeout=5.0),
        ),
    )


class ServerApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "hwabaek.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _store(self):
        from hwabaek.store.sqlite import SQLiteStore

        return SQLiteStore(self.db_path)

    def _submit_provider(self, content: str = "the deliverable"):
        fakes = []

        def provider(team, task):
            fake = SoftScriptedLLM([tool_response("submit_result", {"content": content})])
            fakes.append(fake)
            return lambda spec: fake

        return provider, fakes

    def _blocking_provider(self):
        fakes = []

        def provider(team, task):
            fake = BlockingLLM()
            fakes.append(fake)
            return lambda spec: fake

        return provider, fakes

    def _app(self, *, store=None, provider=None, team_override=None, teams_dir="configs"):
        if provider is None:
            provider, _ = self._submit_provider()
        return create_app(
            store=store,
            teams_dir=teams_dir,
            llm_factory_provider=provider,
            team_override=team_override,
        )

    def _wait_terminal(self, client, sid, timeout=5.0):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            r = client.get(f"/sessions/{sid}")
            self.assertEqual(r.status_code, 200)
            last = r.json()["session"]["status"]
            if last in ("completed", "failed", "cancelled"):
                return r.json()
            time.sleep(0.02)
        raise AssertionError(f"session {sid} not terminal (last status={last})")

    def _wait_persisted(self, client, sid, timeout=5.0):
        """제안 레코드가 store에 반영될 때까지 대기(write-behind flush)."""
        deadline = time.time() + timeout
        detail = None
        while time.time() < deadline:
            detail = client.get(f"/sessions/{sid}").json()
            if detail["proposals"]:
                return detail
            time.sleep(0.02)
        raise AssertionError(f"session {sid} records not persisted; last={detail}")

    # -----------------------------------------------------------------------
    # 1) 정상 생성 -> 완료 -> 상세 조회
    # -----------------------------------------------------------------------
    def test_create_and_complete_session(self) -> None:
        provider, _ = self._submit_provider("final report")
        app = self._app(store=self._store(), provider=provider, team_override=_first_team())
        with TestClient(app) as client:
            r = client.post("/sessions", json={"task": "do the thing"})
            self.assertEqual(r.status_code, 201)
            body = r.json()
            self.assertEqual(body["status"], "running")
            sid = body["id"]

            detail = self._wait_terminal(client, sid)
            self.assertEqual(detail["session"]["status"], "completed")
            self.assertEqual(detail["session"]["result"], "final report")
            self.assertEqual(detail["session"]["submitted_by"], "solo")
            # store는 write-behind라 종료 직후 잠시 뒤에 영속화가 완결된다 —
            # 제안 레코드가 반영될 때까지 짧게 대기해 조회한다.
            detail = self._wait_persisted(client, sid)
            # 메시지 타임라인에 result_proposal이 존재한다.
            types = [m["type"] for m in detail["messages"]]
            self.assertIn("result_proposal", types)
            # 제안 이력 1건.
            self.assertEqual(len(detail["proposals"]), 1)

    # -----------------------------------------------------------------------
    # 2) 세션 목록
    # -----------------------------------------------------------------------
    def test_list_sessions(self) -> None:
        provider, _ = self._submit_provider()
        app = self._app(store=self._store(), provider=provider, team_override=_first_team())
        with TestClient(app) as client:
            r = client.post("/sessions", json={"task": "task one"})
            sid = r.json()["id"]
            self._wait_terminal(client, sid)
            lst = client.get("/sessions")
            self.assertEqual(lst.status_code, 200)
            ids = [s["id"] for s in lst.json()["sessions"]]
            self.assertIn(sid, ids)

    # -----------------------------------------------------------------------
    # 3) 동시 세션 409 (검사+생성 원자화)
    # -----------------------------------------------------------------------
    def test_concurrent_session_conflict(self) -> None:
        provider, _ = self._blocking_provider()
        app = self._app(store=self._store(), provider=provider, team_override=_first_team())
        with TestClient(app) as client:
            r1 = client.post("/sessions", json={"task": "one"})
            self.assertEqual(r1.status_code, 201)
            sid = r1.json()["id"]

            r2 = client.post("/sessions", json={"task": "two"})
            self.assertEqual(r2.status_code, 409)
            self.assertIn("already running", r2.json()["detail"])

            # 취소로 활성 세션을 풀어 준다.
            rc = client.post(f"/sessions/{sid}/cancel")
            self.assertEqual(rc.status_code, 200)
            self.assertEqual(rc.json()["status"], "cancelled")

    # -----------------------------------------------------------------------
    # 4) 취소 후 다시 생성 허용 + 종료 세션 재취소 409
    # -----------------------------------------------------------------------
    def test_cancel_then_recreate_and_double_cancel_conflict(self) -> None:
        provider, _ = self._blocking_provider()
        app = self._app(store=self._store(), provider=provider, team_override=_first_team())
        with TestClient(app) as client:
            r1 = client.post("/sessions", json={"task": "one"})
            sid = r1.json()["id"]
            client.post(f"/sessions/{sid}/cancel")

            # 종료된 세션 재취소 -> 409.
            again = client.post(f"/sessions/{sid}/cancel")
            self.assertEqual(again.status_code, 409)

            # 활성 세션이 없으므로 새 세션 생성 허용.
            r2 = client.post("/sessions", json={"task": "two"})
            self.assertEqual(r2.status_code, 201)
            client.post(f"/sessions/{r2.json()['id']}/cancel")

    # -----------------------------------------------------------------------
    # 5) 없는 세션 404 (조회/취소)
    # -----------------------------------------------------------------------
    def test_unknown_session_404(self) -> None:
        app = self._app(store=self._store(), team_override=_first_team())
        with TestClient(app) as client:
            self.assertEqual(client.get("/sessions/nope").status_code, 404)
            self.assertEqual(client.post("/sessions/nope/cancel").status_code, 404)

    # -----------------------------------------------------------------------
    # 6) 잘못된 body 422
    # -----------------------------------------------------------------------
    def test_bad_body_422(self) -> None:
        app = self._app(store=self._store(), team_override=_first_team())
        with TestClient(app) as client:
            self.assertEqual(client.post("/sessions", json={}).status_code, 422)
            self.assertEqual(
                client.post("/sessions", json={"task": ""}).status_code, 422
            )

    # -----------------------------------------------------------------------
    # 7) 알 수 없는 팀 이름 400
    # -----------------------------------------------------------------------
    def test_unknown_team_400(self) -> None:
        # team_override 없이 실 configs를 쓰되, 존재하지 않는 팀 이름을 요청.
        app = self._app(store=self._store())
        with TestClient(app) as client:
            r = client.post("/sessions", json={"task": "t", "team": "no_such_team"})
            self.assertEqual(r.status_code, 400)
            self.assertIn("unknown team", r.json()["detail"])

    # -----------------------------------------------------------------------
    # 8) GET /teams (실 configs 로드 — default 팀 존재)
    # -----------------------------------------------------------------------
    def test_list_teams_real_configs(self) -> None:
        app = self._app(store=self._store())
        with TestClient(app) as client:
            r = client.get("/teams")
            self.assertEqual(r.status_code, 200)
            names = [t["name"] for t in r.json()["teams"]]
            self.assertIn("default", names)
            default = next(t for t in r.json()["teams"] if t["name"] == "default")
            agent_names = [a["name"] for a in default["agents"]]
            self.assertIn("sangdaedeung", agent_names)
            # 에이전트 요약에 capabilities가 실린다.
            self.assertTrue(all("capabilities" in a for a in default["agents"]))

    # -----------------------------------------------------------------------
    # 9) 서버 시작 시 interrupted 처리 (D-021)
    # -----------------------------------------------------------------------
    def test_startup_marks_interrupted(self) -> None:
        # 이전 실행의 running 세션을 store에 심어 둔다.
        seed_store = self._store()
        old = Session(id="old-sess", task="left running", team_name="demo", created_at=CLOCK)
        asyncio.run(seed_store.save_session(old))
        asyncio.run(seed_store.close())

        app = self._app(store=self._store(), team_override=_first_team())
        with TestClient(app) as client:
            r = client.get("/sessions/old-sess")
            self.assertEqual(r.status_code, 200)
            s = r.json()["session"]
            self.assertEqual(s["status"], "failed")
            self.assertEqual(s["fail_reason"], "interrupted")
            self.assertIsNotNone(s["finished_at"])


if __name__ == "__main__":
    unittest.main()

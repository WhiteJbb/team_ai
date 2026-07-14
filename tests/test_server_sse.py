"""M3 서버 SSE 스트림 테스트 — 재전송 + Last-Event-ID + 라이브 스트림 경로.

밀폐: LLM은 스크립트 Fake 주입, 실네트워크/실키 없음. fastapi TestClient의
스트리밍 응답으로 SSE 프레임을 파싱해 sequence 기준 재전송/재구독 규약
(EventContract §5)을 검증한다.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from hwabaek.contracts import (
    AgentSpec,
    ApprovalConfig,
    ApprovalPolicy,
    Event,
    EventType,
    TeamConfig,
    TerminationPolicy,
)
from hwabaek.llm.fake import text_response, tool_response
from hwabaek.server import create_app
from hwabaek.server.events import SessionRegistry, SessionRunner


class SoftScriptedLLM:
    """스크립트 소비 후 idle 텍스트를 돌려주는 대역 (하드 소진 회피)."""

    def __init__(self, script) -> None:
        self._script = list(script)
        self._i = 0

    async def complete(self, request):
        if self._i < len(self._script):
            item = self._script[self._i]
            self._i += 1
            if isinstance(item, BaseException):
                raise item
            return item
        return text_response("(idle)")


def _first_team() -> TeamConfig:
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


def _idle_team() -> TeamConfig:
    """2인 unanimous 팀 — 아무도 제출하지 않아 idle_timeout 후 failed(idle)."""
    return TeamConfig(
        name="idlers",
        agents=(
            AgentSpec(name="a", role="tester", system_prompt="Test agent a."),
            AgentSpec(name="b", role="tester", system_prompt="Test agent b."),
        ),
        termination=TerminationPolicy(
            max_messages=20,
            token_budget=100_000,
            idle_timeout=0.3,
            approval=ApprovalConfig(mode=ApprovalPolicy.UNANIMOUS, voting_timeout=1.0),
        ),
    )


def _parse_sse(client, url, headers=None):
    """SSE 스트림을 끝까지 읽어 이벤트 dict 목록으로 파싱한다."""
    events = []
    with client.stream("GET", url, headers=headers or {}) as r:
        assert r.status_code == 200, r.status_code
        assert "text/event-stream" in r.headers["content-type"]
        cur: dict = {}
        for line in r.iter_lines():
            if line == "":
                if cur:
                    events.append(cur)
                    cur = {}
                continue
            if line.startswith("id:"):
                cur["id"] = int(line[3:].strip())
            elif line.startswith("event:"):
                cur["event"] = line[6:].strip()
            elif line.startswith("data:"):
                cur["data"] = json.loads(line[5:].strip())
        if cur:
            events.append(cur)
    return events


class ServerSseTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "hwabaek.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _store(self):
        from hwabaek.store.sqlite import SQLiteStore

        return SQLiteStore(self.db_path)

    def _app(self, *, provider, team_override):
        return create_app(
            store=self._store(),
            teams_dir="configs",
            llm_factory_provider=provider,
            team_override=team_override,
        )

    def _submit_provider(self, content="deliverable"):
        def provider(team, task):
            fake = SoftScriptedLLM([tool_response("submit_result", {"content": content})])
            return lambda spec: fake

        return provider

    def _idle_provider(self):
        def provider(team, task):
            return lambda spec: SoftScriptedLLM([])

        return provider

    # -----------------------------------------------------------------------
    # 1) 재전송(full replay) + Last-Event-ID 재개
    # -----------------------------------------------------------------------
    def test_replay_and_last_event_id_resume(self) -> None:
        app = self._app(provider=self._submit_provider("final"), team_override=_first_team())
        with TestClient(app) as client:
            r = client.post("/sessions", json={"task": "go"})
            sid = r.json()["id"]

            # 전체 재전송 — 스트림 소비가 세션 완료까지 대기한다(종료 후 종결).
            events = _parse_sse(client, f"/sessions/{sid}/events")
            self.assertTrue(events)
            seqs = [e["id"] for e in events]
            self.assertEqual(seqs, sorted(seqs))
            # sequence는 0부터 단조 증가.
            self.assertEqual(seqs[0], 0)
            types = [e["event"] for e in events]
            self.assertIn("session_status", types)
            self.assertIn("result", types)
            # 마지막 세션 상태는 completed.
            statuses = [
                e["data"]["payload"]["status"]
                for e in events
                if e["event"] == "session_status"
            ]
            self.assertEqual(statuses[-1], "completed")

            # Last-Event-ID로 재개 — 그 sequence 초과분만 반환된다.
            cutoff = seqs[len(seqs) // 2]
            resumed = _parse_sse(
                client,
                f"/sessions/{sid}/events",
                headers={"Last-Event-ID": str(cutoff)},
            )
            self.assertTrue(all(e["id"] > cutoff for e in resumed))
            self.assertEqual(
                [e["id"] for e in resumed],
                [s for s in seqs if s > cutoff],
            )

    # -----------------------------------------------------------------------
    # 2) 라이브 스트림 (running 중 구독 -> 종료까지 수신)
    # -----------------------------------------------------------------------
    def test_live_stream_until_terminal(self) -> None:
        app = self._app(provider=self._idle_provider(), team_override=_idle_team())
        with TestClient(app) as client:
            r = client.post("/sessions", json={"task": "idle out"})
            sid = r.json()["id"]

            # POST 직후 구독 -> running 라이브 이벤트부터 failed(idle)까지 수신.
            events = _parse_sse(client, f"/sessions/{sid}/events")
            self.assertTrue(events)
            statuses = [
                e["data"]["payload"]["status"]
                for e in events
                if e["event"] == "session_status"
            ]
            self.assertEqual(statuses[0], "running")
            self.assertEqual(statuses[-1], "failed")
            fail_reason = [
                e["data"]["payload"]["fail_reason"]
                for e in events
                if e["event"] == "session_status" and e["data"]["payload"]["status"] == "failed"
            ]
            self.assertEqual(fail_reason[-1], "idle")

    # -----------------------------------------------------------------------
    # 3) 없는 세션 SSE 404
    # -----------------------------------------------------------------------
    def test_events_unknown_session_404(self) -> None:
        app = self._app(provider=self._submit_provider(), team_override=_first_team())
        with TestClient(app) as client:
            r = client.get("/sessions/nope/events")
            self.assertEqual(r.status_code, 404)


def _evt(seq: int) -> Event:
    return Event(
        event_id=f"evt-{seq}",
        session_id="sess",
        type=EventType.SESSION_STATUS,
        sequence=seq,
        created_at=CLOCK,
        payload={"status": "running", "fail_reason": None, "fail_detail": None},
    )


CLOCK = "2026-07-14T00:00:00Z"


class SseReplayRaceTest(unittest.IsolatedAsyncioTestCase):
    """재전송(replay) 도중 러너가 이벤트를 더 내고 종료하는 경합에서 이벤트가
    누락/중복되지 않는지 결정적으로 검증한다 (fresh-eyes 회귀).

    _stream_from_runner의 snapshot은 첫 yield에서 중단되는 사이에 낡을 수 있다 —
    그 뒤 도착한 이벤트도 라이브 큐 배수로 전달되어야 한다."""

    def _registry(self) -> SessionRegistry:
        return SessionRegistry(
            store=None,
            teams_dir="configs",
            llm_factory_provider=lambda team, task: (lambda spec: None),
            clock=lambda: CLOCK,
            id_factory_provider=lambda: (lambda: "x"),
        )

    def _finish(self, runner: SessionRunner) -> None:
        """_run의 종료 처리를 재현한다 — done 표시 + 구독자 센티널."""
        runner._done.set()
        for q in list(runner._subscribers):
            q.put_nowait(None)

    @staticmethod
    def _seqs(chunks) -> list[int]:
        out = []
        for chunk in chunks:
            for line in chunk.splitlines():
                if line.startswith("id:"):
                    out.append(int(line[3:].strip()))
        return out

    async def test_events_during_replay_are_not_dropped(self) -> None:
        runner = SessionRunner()
        for seq in (0, 1, 2):
            runner.on_event(_evt(seq))
        registry = self._registry()

        agen = registry._stream_from_runner(runner, -1)
        # 첫 청크: 구독 + snapshot([0,1,2]) + yield(0). 이 시점에 snapshot은 고정된다.
        first = await agen.__anext__()
        # 재전송 중 러너가 새 이벤트를 내고 종료한다 (snapshot에는 없음, 큐에만 있음).
        runner.on_event(_evt(3))
        runner.on_event(_evt(4))
        self._finish(runner)

        chunks = [first]
        async for chunk in agen:
            chunks.append(chunk)

        seqs = self._seqs(chunks)
        # 0..4 전부, 순서대로, 중복 없이.
        self.assertEqual(seqs, [0, 1, 2, 3, 4])

    async def test_subscribe_after_done_gets_full_replay_then_ends(self) -> None:
        # 러너가 이미 종료된 뒤 구독해도 재전송 후 즉시 종료해야 한다(무한 대기 금지).
        runner = SessionRunner()
        for seq in (0, 1, 2):
            runner.on_event(_evt(seq))
        self._finish(runner)
        registry = self._registry()

        chunks = [c async for c in registry._stream_from_runner(runner, -1)]
        self.assertEqual(self._seqs(chunks), [0, 1, 2])

    async def test_last_event_id_skips_replayed_prefix(self) -> None:
        runner = SessionRunner()
        for seq in (0, 1, 2, 3):
            runner.on_event(_evt(seq))
        self._finish(runner)
        registry = self._registry()

        chunks = [c async for c in registry._stream_from_runner(runner, 1)]
        self.assertEqual(self._seqs(chunks), [2, 3])


if __name__ == "__main__":
    unittest.main()

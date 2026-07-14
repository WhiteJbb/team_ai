"""hwabaek 로컬 서버 — FastAPI REST/SSE와 M4 정적 대시보드.

세션 생성/조회/취소·팀 조회·SSE 이벤트 스트림과 `/app/` UI를 제공한다.
조립은 create_app, 진입점은 `python -m hwabaek.serve`.
"""
from hwabaek.server.app import create_app, default_clock, make_id_factory

__all__ = ["create_app", "default_clock", "make_id_factory"]

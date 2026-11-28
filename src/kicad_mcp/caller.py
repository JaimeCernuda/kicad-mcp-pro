"""Which caller a tool call belongs to.

One server serves every agent of a workspace. KiCad keeps one commit per IPC client name
(``m_commits[ClientName]`` in its API handler), so each caller's IPC requests carry their
own client name and each caller owns its own edit group.

The caller comes from the call's ``_meta``: CLIO sends ``ai.iowarp.clio/caller`` =
``{"session", "root", "via"}`` and the owner is ``session``. Other clients are told apart by
their MCP transport session; a client with neither is the single default caller.
"""

from __future__ import annotations

import contextvars
from collections.abc import Mapping
from typing import Any, cast

from mcp.server.context import CallNext, HandlerResult, ServerMiddleware, ServerRequestContext

CALLER_META_KEY = "ai.iowarp.clio/caller"
_MAX_CALLER_LENGTH = 120

_CALLER: contextvars.ContextVar[str] = contextvars.ContextVar("kicad_mcp_caller", default="")


def current_caller() -> str:
    """The caller of the tool call in progress; empty for the default caller."""
    return _CALLER.get()


def caller_from_request(meta: object, transport_session: object) -> str:
    """The caller named by a request's ``_meta``, else its transport session."""
    stamped = (
        cast(Mapping[str, object], meta).get(CALLER_META_KEY) if isinstance(meta, Mapping) else None
    )
    if isinstance(stamped, Mapping):
        session = cast(Mapping[str, object], stamped).get("session")
        if isinstance(session, str) and session.strip():
            return session.strip()[:_MAX_CALLER_LENGTH]
    return transport_session[:_MAX_CALLER_LENGTH] if isinstance(transport_session, str) else ""


class CallerMiddleware(ServerMiddleware[Any]):
    """Bind each tool call to its caller for the duration of the call.

    Sync tools run in worker threads, which receive a copy of this context.
    """

    async def __call__(
        self,
        context: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> HandlerResult:
        if context.method != "tools/call":
            return await call_next(context)
        # The SDK keeps the transport session on the session's connection; stdio has none.
        connection = getattr(context.session, "_connection", None)
        token = _CALLER.set(
            caller_from_request(context.meta, getattr(connection, "session_id", None))
        )
        try:
            return await call_next(context)
        finally:
            _CALLER.reset(token)


__all__ = ["CALLER_META_KEY", "CallerMiddleware", "caller_from_request", "current_caller"]

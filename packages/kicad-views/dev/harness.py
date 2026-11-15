"""A minimal MCP Apps host for developing the KiCad views against a real server.

    python packages/kicad-views/dev/harness.py <project folder> [--port 8765]

It starts ``kicad-mcp-pro`` over stdio with the views enabled, serves a page that opens
a view in an iframe and answers the view's requests the way an MCP Apps host does, and
forwards the view's tool calls to the server. Open ``/?view=board`` (or schematic, bom,
checks, stackup, parts) and pass the opening tool's arguments as ``&args=<json>``.
The page exposes ``window.harness`` (the last context, app-tool calls) for browser tests.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
from collections.abc import Coroutine
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import parse_qs, urlparse

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

T = TypeVar("T")

HOST_PAGE = (Path(__file__).parent / "harness.html").read_text(encoding="utf-8")


class Server:
    def __init__(self, project: Path) -> None:
        self.project = project
        self.loop = asyncio.new_event_loop()
        self.session: ClientSession | None = None
        self.ready = threading.Event()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(self._run(), self.loop)

    async def _run(self) -> None:
        env = {
            **os.environ,
            "KICAD_MCP_ENABLE_APPS": "1",
            "KICAD_MCP_PROFILE": os.environ.get("KICAD_MCP_PROFILE", "full"),
            "KICAD_MCP_OPERATING_MODE": os.environ.get("KICAD_MCP_OPERATING_MODE", "experimental"),
            "KICAD_MCP_KICAD_INSTANCE": os.environ.get("KICAD_MCP_KICAD_INSTANCE", "auto"),
            "KICAD_MCP_FILTER_RUNTIME_TOOLS": "false",
        }
        params = StdioServerParameters(
            command=sys.executable,
            args=["-c", "from kicad_mcp.server import main; main()", "--transport", "stdio"],
            env=env,
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            await session.call_tool("kicad_set_project", {"project_dir": str(self.project)})
            self.session = session
            self.ready.set()
            await asyncio.Event().wait()

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        self.ready.wait(120)
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(300)


def handler(server: Server) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def _send(self, body: bytes, kind: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if url.path == "/resource":
                uri = parse_qs(url.query)["uri"][0]
                session = server.session
                assert session is not None  # noqa: S101 - set before the first request
                result = server.run(session.read_resource(uri))
                self._send(result.contents[0].text.encode(), "text/html")
                return
            self._send(HOST_PAGE.encode(), "text/html")

        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            result = server.run(server.session.call_tool(body["name"], body.get("arguments") or {}))  # type: ignore[union-attr]
            self._send(
                json.dumps(result.model_dump(by_alias=True, exclude_none=True)).encode(),
                "application/json",
            )

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = Server(args.project.resolve())
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), handler(server))
    print(f"harness on http://127.0.0.1:{args.port}/?view=board", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()

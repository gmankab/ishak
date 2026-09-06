#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.14"
# dependencies = ["mcp"]
# tool.ty.environment.python = ".venv"
# ///

from __future__ import annotations

import atexit
import json
import os
import queue
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
from collections import deque
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any, BinaryIO, TextIO, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

mcp = MCPServer(
    "codex-app-mcp",
    instructions=(
        "schema discovers the installed Codex API; rpc calls any app-server method; "
        "events reads notifications/server requests; respond answers server requests; "
        "code batches rpc/events calls inside the official Code Mode Host."
    ),
)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class App:
    def __init__(self) -> None:
        self.codex = os.environ.get("CODEX_BIN") or shutil.which("codex") or "codex"
        self.home = Path(os.environ.get("CODEX_HOME", "~/.local/state/codex-mcp")).expanduser()
        self.home.mkdir(parents=True, exist_ok=True)
        extra = cast(list[str], json.loads(os.environ.get("CODEX_APP_SERVER_ARGS", "[]")))
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.home)
        self.proc = subprocess.Popen(
            [self.codex, "app-server", "--stdio", *extra], cwd="/", env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr,
            text=True, bufsize=1,
        )
        self.stdin = cast(TextIO, self.proc.stdin)
        self.stdout = cast(TextIO, self.proc.stdout)
        self.write_lock = threading.Lock()
        self.state = threading.Condition()
        self.next_id = 1
        self.seq = 0
        self.dead = False
        self.pending: dict = {}
        self.log: deque[tuple] = deque(maxlen=4096)
        threading.Thread(target=self._reader, daemon=True).start()
        self.call("initialize", {
            "clientInfo": {"name": "codex-app-mcp", "title": "Codex App MCP", "version": "1"},
            "capabilities": {"experimentalApi": True, "requestAttestation": False},
        }, 60_000)
        self.notify("initialized")

    def _send(self, message: dict) -> None:
        if self.proc.poll() is not None:
            raise ToolError("codex app-server is not running")
        with self.write_lock:
            self.stdin.write(dumps(message) + "\n")
            self.stdin.flush()

    def _reader(self) -> None:
        try:
            for line in self.stdout:
                message = cast(dict, json.loads(line))
                if "method" in message:
                    self.event(message)
                    continue
                with self.state:
                    pending = self.pending.get(cast(int, message["id"]))
                if pending is not None:
                    pending.put(message)
        except (json.JSONDecodeError, KeyError, OSError, TypeError) as exc:
            print(f"app-server reader: {exc}", file=sys.stderr)
        finally:
            with self.state:
                self.dead = True
                for pending in self.pending.values():
                    pending.put(None)
                self.state.notify_all()

    def event(self, message: dict) -> None:
        with self.state:
            self.seq += 1
            self.log.append((self.seq, message))
            self.state.notify_all()

    def call(self, method: str, params: dict | None = None, timeout_ms: int | None = 30_000) -> Any:
        with self.state:
            rpc_id = self.next_id
            self.next_id += 1
            pending: queue.Queue[dict | None] = queue.Queue()
            self.pending[rpc_id] = pending
        try:
            self._send({"id": rpc_id, "method": method, "params": params or {}})
            try:
                message = pending.get(timeout=None if timeout_ms is None else timeout_ms / 1000)
            except queue.Empty as exc:
                raise ToolError(f"app-server request timed out: {method}") from exc
            if message is None:
                raise ToolError("codex app-server exited")
            if message.get("error") is not None:
                raise ToolError(dumps(message["error"]))
            return message.get("result")
        finally:
            with self.state:
                self.pending.pop(rpc_id, None)

    def notify(self, method: str, params: dict | None = None) -> None:
        message: dict = {"method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def respond(self, request_id, result: Any = None, error: Any = None) -> dict:
        if result is not None and error is not None:
            raise ToolError("set result or error, not both")
        self._send({"id": request_id, "error": error} if error is not None else {"id": request_id, "result": result})
        return {"ok": True}

    def events(self, cursor: int = 0, wait_ms: int = 0, limit: int = 100) -> dict:
        if cursor < 0 or wait_ms < 0 or limit < 1:
            raise ToolError("cursor/wait_ms must be >= 0 and limit > 0")
        with self.state:
            if self.seq <= cursor and wait_ms:
                self.state.wait_for(lambda: self.seq > cursor or self.dead, timeout=wait_ms / 1000)
            rows = [(seq, msg) for seq, msg in self.log if seq > cursor][:limit]
            return {
                "cursor": rows[-1][0] if rows else cursor,
                "latest": self.seq,
                "oldest": self.log[0][0] if self.log else self.seq + 1,
                "events": [{"seq": seq, "message": msg} for seq, msg in rows],
            }

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(2)
            except subprocess.TimeoutExpired:
                self.proc.kill()


APP = App()
atexit.register(APP.close)


def _rpc(method: str, params: dict[str, Any] | None = None, notification: bool = False,
         timeout_ms: int | None = 30_000) -> Any:
    if notification:
        APP.notify(method, params)
        return {"ok": True}
    return APP.call(method, params, timeout_ms)


@mcp.tool()
def rpc(method: str, params: dict[str, Any] | None = None, notification: bool = False,
        timeout_ms: int | None = 30_000) -> str:
    """Call any Codex app-server JSON-RPC method. process/spawn is unsandboxed."""
    return dumps(_rpc(method, params, notification, timeout_ms))


@mcp.tool()
def events(cursor: int = 0, wait_ms: int = 0, limit: int = 100) -> str:
    """Read app-server notifications and server-initiated requests by cursor."""
    return dumps(APP.events(cursor, wait_ms, limit))


@mcp.tool()
def respond(request_id: int | str, result: Any = None, error: Any = None) -> str:
    """Respond to an app-server request such as approval, input or elicitation."""
    return dumps(APP.respond(request_id, result, error))


@cache
def _schemas() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="codex-schema-") as tmp:
        env = os.environ.copy()
        env["CODEX_HOME"] = str(APP.home)
        run = subprocess.run(
            [APP.codex, "app-server", "generate-json-schema", "--experimental", "--out", tmp],
            cwd="/", env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        if run.returncode:
            raise ToolError(run.stderr.strip() or "schema generation failed")
        root = Path(tmp)
        return {str(path.relative_to(root)): path.read_text(encoding="utf-8") for path in root.rglob("*.json")}


@mcp.tool()
def schema(query: str = "") -> str:
    """Search exact experimental schemas generated by the installed Codex binary."""
    files = _schemas()
    if not query:
        return dumps({"files": sorted(files)})
    needle = query.casefold()
    matches: list[dict] = []
    for name, text in files.items():
        pos = text.casefold().find(needle)
        if pos >= 0:
            matches.append({"file": name, "snippet": text[max(0, pos - 700):pos + len(query) + 1300]})
        if len(matches) == 8:
            break
    return dumps({"query": query, "matches": matches})


NESTED: dict[str, Callable[..., Any]] = {
    "rpc": _rpc,
    "events": APP.events,
    "respond": APP.respond,
}
CODE_TOOLS: list[dict] = [
    {"name": "rpc", "tool_name": {"name": "rpc", "namespace": None}, "description": "Call any app-server method.",
     "kind": "function", "input_schema": {"type": "object", "properties": {
         "method": {"type": "string"}, "params": {"type": "object"}, "notification": {"type": "boolean"},
         "timeout_ms": {"type": ["integer", "null"]}}, "required": ["method"]}, "output_schema": None},
    {"name": "events", "tool_name": {"name": "events", "namespace": None}, "description": "Read app-server events.",
     "kind": "function", "input_schema": {"type": "object", "properties": {
         "cursor": {"type": "integer"}, "wait_ms": {"type": "integer"}, "limit": {"type": "integer"}}},
     "output_schema": None},
    {"name": "respond", "tool_name": {"name": "respond", "namespace": None}, "description": "Answer app-server request.",
     "kind": "function", "input_schema": {"type": "object", "properties": {
         "request_id": {"anyOf": [{"type": "integer"}, {"type": "string"}]}, "result": {}, "error": {}},
         "required": ["request_id"]}, "output_schema": None},
]


def _code_binary() -> str:
    explicit = os.environ.get("CODEX_CODE_MODE_HOST_BIN")
    if explicit:
        return explicit
    codex = Path(shutil.which(APP.codex) or APP.codex).resolve()
    sibling = codex.with_name("codex-code-mode-host")
    if sibling.is_file():
        return str(sibling)
    found = shutil.which("codex-code-mode-host")
    if found:
        return found
    raise ToolError("codex-code-mode-host not found; install the version-matched companion")


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise ToolError("code-mode host exited")
        data.extend(chunk)
    return bytes(data)


class Code:
    def __init__(self) -> None:
        self.proc = subprocess.Popen([_code_binary(), "--listen", "stdio://"], cwd="/",
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr)
        self.stdin = cast(BinaryIO, self.proc.stdin)
        self.stdout = cast(BinaryIO, self.proc.stdout)
        self.lock = threading.Lock()
        self.next_id = 1
        self.send({"type": "connection/hello", "supportedVersions": [1],
                   "requiredCapabilities": [], "optionalCapabilities": []})
        hello = self.recv()
        if hello.get("type") != "connection/ready":
            raise ToolError(f"code-mode handshake failed: {dumps(hello)}")
        rpc_id = self.ident()
        self.send({"type": "operation/request", "id": rpc_id,
                   "request": {"method": "session/open", "sessionId": "mcp"}})
        self.wait(rpc_id, "operation/response")

    def ident(self) -> int:
        rpc_id = self.next_id
        self.next_id += 1
        return rpc_id

    def send(self, message: dict) -> None:
        payload = dumps(message).encode()
        if len(payload) > 64 * 1024 * 1024:
            raise ToolError("code-mode frame too large")
        with self.lock:
            self.stdin.write(struct.pack("<I", len(payload)) + payload)
            self.stdin.flush()

    def recv(self) -> dict:
        size = struct.unpack("<I", _read_exact(self.stdout, 4))[0]
        if size > 64 * 1024 * 1024:
            raise ToolError("code-mode frame too large")
        return cast(dict, json.loads(_read_exact(self.stdout, size)))

    def delegate(self, message: dict) -> None:
        try:
            request = cast(dict, message["request"])
            if request["type"] == "notification/send":
                APP.event({"method": "code/notification", "params": request})
                value: Any = {"type": "notification/delivered"}
            else:
                invocation = cast(dict, request["invocation"])
                name = cast(dict, invocation["tool_name"])["name"]
                args = cast(dict, invocation.get("input") or {})
                value = {"type": "tool/result", "result": NESTED[name](**args)}
            result: Any = {"status": "ok", "value": value}
        except Exception as exc:
            result = {"status": "error", "message": str(exc)}
        try:
            self.send({"type": "delegate/response", "id": message["id"], "result": result})
        except (BrokenPipeError, OSError):
            pass

    def wait(self, rpc_id: int, kind: str) -> Any:
        while True:
            message = self.recv()
            if message.get("type") == "delegate/request":
                threading.Thread(target=self.delegate, args=(message,), daemon=True).start()
                continue
            if message.get("type") == "cell/closed":
                APP.event({"method": "code/cellClosed", "params": message})
                continue
            if message.get("type") == kind and message.get("id") == rpc_id:
                result = cast(dict, message["result"])
                if result["status"] == "error":
                    raise ToolError(str(result["message"]))
                return result["value"]

    def execute(self, source: str, yield_time_ms: int, max_output_tokens: int | None) -> Any:
        rpc_id = self.ident()
        self.send({"type": "operation/request", "id": rpc_id, "request": {
            "method": "session/execute", "sessionId": "mcp", "request": {
                "tool_call_id": f"mcp-{rpc_id}", "enabled_tools": CODE_TOOLS, "source": source,
                "yield_time_ms": yield_time_ms, "max_output_tokens": max_output_tokens}}})
        value = self.wait(rpc_id, "execute/initialResponse")
        while "Yielded" in cast(dict, value):
            body = cast(dict, cast(dict, value)["Yielded"])
            wait_id = self.ident()
            self.send({"type": "operation/request", "id": wait_id, "request": {
                "method": "session/wait", "sessionId": "mcp",
                "request": {"cell_id": body["cell_id"], "yield_time_ms": yield_time_ms}}})
            waited = cast(dict, self.wait(wait_id, "operation/response"))
            outcome = cast(dict, waited["outcome"])
            value = outcome.get("LiveCell") or outcome.get("MissingCell")
        return value

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(2)
            except subprocess.TimeoutExpired:
                self.proc.kill()


@mcp.tool()
def code(source: str, yield_time_ms: int = 30_000, max_output_tokens: int | None = None) -> str:
    """Run JavaScript in the official Code Mode Host with tools.rpc/events/respond."""
    host = Code()
    try:
        return dumps(host.execute(source, yield_time_ms, max_output_tokens))
    finally:
        host.close()


if __name__ == "__main__":
    mcp.run()

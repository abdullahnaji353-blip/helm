"""Small stdio MCP client compatible with the project's Python 3.9 runtime."""

from __future__ import annotations

import json
import logging
import os
import select
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any


_LOGGER = logging.getLogger("local_desktop_assistant.mcp")
_LOGGER.addHandler(logging.NullHandler())


class MCPError(RuntimeError):
    pass


class StdioMCPServer:
    def __init__(self, name: str, config: dict[str, Any], timeout: float = 10) -> None:
        self.name = name
        self.config = config
        self.timeout = timeout
        self.process: subprocess.Popen[str] | None = None
        self.next_request_id = 1
        self.tools: list[dict[str, Any]] = []
        self._stderr: deque[str] = deque(maxlen=40)
        self._stderr_thread: threading.Thread | None = None
        self._stdout_buffer = bytearray()

    def connect(self) -> list[dict[str, Any]]:
        command = self.config.get("command")
        args = self.config.get("args", [])
        env = self.config.get("env", {})
        cwd = self.config.get("cwd")
        if not isinstance(command, str) or not command.strip():
            raise MCPError(f"MCP server '{self.name}' needs a non-empty command.")
        if (
            not isinstance(args, list)
            or not all(isinstance(arg, str) for arg in args)
            or not isinstance(env, dict)
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in env.items())
            or cwd is not None and not isinstance(cwd, str)
        ):
            raise MCPError(f"MCP server '{self.name}' has invalid args, env, or cwd.")
        if cwd is not None and not Path(cwd).is_dir():
            raise MCPError(f"MCP server '{self.name}' working directory does not exist: {cwd}")

        child_env = os.environ.copy()
        child_env.update(env)
        _LOGGER.info(
            "Starting configured MCP process server=%s command=%r args_count=%d cwd=%r",
            self.name,
            command,
            len(args),
            cwd,
        )
        try:
            self.process = subprocess.Popen(
                [command, *args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                cwd=cwd,
                env=child_env,
            )
        except OSError as error:
            _LOGGER.exception("Could not start MCP process server=%s", self.name)
            raise MCPError(f"Could not start MCP server '{self.name}': {error}") from error

        _LOGGER.info(
            "Started configured MCP process server=%s pid=%s",
            self.name,
            self.process.pid,
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True
        )
        self._stderr_thread.start()
        try:
            initialization = self._request(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "local-desktop-assistant", "version": "0.1.0"},
                },
            )
            if not isinstance(initialization.get("protocolVersion"), str):
                raise MCPError(
                    f"MCP server '{self.name}' did not negotiate a protocol version."
                )
            _LOGGER.info(
                "MCP handshake complete server=%s protocol=%s",
                self.name,
                initialization["protocolVersion"],
            )
            self._notify("notifications/initialized", {})
            cursor: str | None = None
            for _ in range(100):
                params: dict[str, Any] = {}
                if cursor:
                    params["cursor"] = cursor
                result = self._request("tools/list", params)
                tools = result.get("tools")
                if not isinstance(tools, list):
                    raise MCPError(
                        f"MCP server '{self.name}' returned an invalid tools/list response."
                    )
                self.tools.extend(tool for tool in tools if isinstance(tool, dict))
                _LOGGER.info(
                    "MCP tools/list response server=%s count=%d names=%s has_next_page=%s",
                    self.name,
                    len(tools),
                    [tool.get("name", "") for tool in tools if isinstance(tool, dict)],
                    "nextCursor" in result,
                )
                next_cursor = result.get("nextCursor")
                if next_cursor is None:
                    break
                if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                    raise MCPError(
                        f"MCP server '{self.name}' returned an invalid pagination cursor."
                    )
                cursor = next_cursor
            else:
                raise MCPError(
                    f"MCP server '{self.name}' exceeded the tool pagination limit."
                )
            return self.tools
        except Exception:
            _LOGGER.exception("MCP initialization/listing failed server=%s", self.name)
            self.close()
            raise

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._request(
            "tools/call", {"name": name, "arguments": arguments}
        )
        if not isinstance(result, dict):
            raise MCPError(f"MCP server '{self.name}' returned an invalid tool result.")
        content = result.get("content")
        _LOGGER.info(
            "MCP tool call response server=%s tool=%s is_error=%s content_items=%d",
            self.name,
            name,
            result.get("isError", False),
            len(content) if isinstance(content, list) else 0,
        )
        return result

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.process is None or self.process.poll() is not None:
            raise MCPError(self._failure_message("process is not running"))
        request_id = self.next_request_id
        self.next_request_id += 1
        _LOGGER.info(
            "Sending MCP JSON-RPC request server=%s method=%s id=%d",
            self.name,
            method,
            request_id,
        )
        self._write({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        })
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            message = self._read_message(deadline)
            if message is None:
                break
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"]
                if isinstance(error, dict):
                    detail = error.get("message", "unknown protocol error")
                else:
                    detail = str(error)
                _LOGGER.error(
                    "MCP JSON-RPC error server=%s method=%s id=%d detail=%s",
                    self.name,
                    method,
                    request_id,
                    detail,
                )
                raise MCPError(f"MCP server '{self.name}' {method} failed: {detail}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise MCPError(f"MCP server '{self.name}' returned an invalid {method} result.")
            if method == "initialize":
                _LOGGER.info(
                    "Received MCP handshake response server=%s id=%d",
                    self.name,
                    request_id,
                )
            return result
        _LOGGER.error(
            "MCP JSON-RPC request timed out server=%s method=%s id=%d",
            self.name,
            method,
            request_id,
        )
        raise MCPError(self._failure_message(f"timed out waiting for {method}"))

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _write(self, message: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise MCPError(f"MCP server '{self.name}' input is closed.")
        try:
            encoded = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
            self.process.stdin.write(encoded)
            self.process.stdin.flush()
        except (OSError, BrokenPipeError) as error:
            raise MCPError(self._failure_message(f"could not write protocol message: {error}")) from error

    def _read_message(self, deadline: float) -> dict[str, Any] | None:
        if self.process is None or self.process.stdout is None:
            raise MCPError(f"MCP server '{self.name}' output is closed.")
        while b"\n" not in self._stdout_buffer:
            if len(self._stdout_buffer) > 10_000_000:
                raise MCPError(self._failure_message("sent an oversized protocol message"))
            remaining = max(0, deadline - time.monotonic())
            ready, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not ready:
                return None
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise MCPError(self._failure_message("closed its output unexpectedly"))
            self._stdout_buffer.extend(chunk)
        line, _, remainder = self._stdout_buffer.partition(b"\n")
        self._stdout_buffer = bytearray(remainder)
        if len(line) > 10_000_000:
            raise MCPError(self._failure_message("sent an oversized protocol message"))
        try:
            message = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise MCPError(
                self._failure_message("wrote non-JSON data to stdout")
            ) from error
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise MCPError(self._failure_message("wrote an invalid JSON-RPC message"))
        return message

    def _drain_stderr(self) -> None:
        if self.process is None or self.process.stderr is None:
            return
        for line in self.process.stderr:
            self._stderr.append(line.decode("utf-8", errors="replace").rstrip())

    def _failure_message(self, reason: str) -> str:
        detail = "\n".join(self._stderr)
        suffix = f"\nServer log: {detail}" if detail else ""
        return f"MCP server '{self.name}' {reason}.{suffix}"

    def close(self) -> None:
        child = self.process
        self.process = None
        if child is None:
            return
        _LOGGER.info(
            "Closing configured MCP process server=%s pid=%s",
            self.name,
            child.pid,
        )
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                stream.close()
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=0.2)
            self._stderr_thread = None


class MCPConnector:
    def __init__(self, config_path: str | Path, timeout: float = 10) -> None:
        self.config_path = Path(config_path).expanduser()
        self.timeout = timeout
        self.servers: dict[str, StdioMCPServer] = {}

    def connect(self) -> dict[str, Any]:
        _LOGGER.info("Reading MCP config path=%s", self.config_path)
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _LOGGER.exception("MCP config file is missing path=%s", self.config_path)
            raise MCPError(
                f"MCP config not found: {self.config_path}. Create it using the example."
            )
        except (OSError, json.JSONDecodeError) as error:
            _LOGGER.exception("Could not read MCP config path=%s", self.config_path)
            raise MCPError(f"Could not read MCP config {self.config_path}: {error}") from error
        if not isinstance(data, dict) or not isinstance(data.get("mcpServers"), dict):
            _LOGGER.error("MCP config has an invalid top-level structure")
            raise MCPError("MCP config must contain an object named 'mcpServers'.")

        errors: dict[str, str] = {}
        _LOGGER.info(
            "MCP config parsed server_count=%d names=%s",
            len(data["mcpServers"]),
            list(data["mcpServers"]),
        )
        if not data["mcpServers"]:
            _LOGGER.warning("MCP config contains no configured servers")
        for name, config in data["mcpServers"].items():
            if not isinstance(name, str) or not isinstance(config, dict):
                errors[str(name)] = "Server name/config must be an object."
                continue
            server = StdioMCPServer(name, config, timeout=self.timeout)
            try:
                server.connect()
            except MCPError as error:
                _LOGGER.error(
                    "MCP server connection failed server=%s error=%s",
                    name,
                    error,
                )
                errors[name] = str(error)
                continue
            self.servers[name] = server
        _LOGGER.info(
            "MCP connection summary connected=%d failed=%d",
            len(self.servers),
            len(errors),
        )
        return {
            "servers": [
                {
                    "name": name,
                    "tools": [
                        {
                            "name": tool.get("name", ""),
                            "description": tool.get("description", ""),
                            "inputSchema": tool.get("inputSchema", {}),
                        }
                        for tool in server.tools
                    ],
                }
                for name, server in self.servers.items()
            ],
            "errors": errors,
        }

    def call_tool(
        self, server_name: str, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        server = self.servers.get(server_name)
        if server is None:
            raise MCPError(f"MCP server '{server_name}' is not connected.")
        if not any(tool.get("name") == tool_name for tool in server.tools):
            raise MCPError(f"Tool '{tool_name}' is not advertised by '{server_name}'.")
        _LOGGER.info(
            "Calling MCP tool server=%s tool=%s argument_keys=%s",
            server_name,
            tool_name,
            sorted(arguments),
        )
        return server.call_tool(tool_name, arguments)

    def close(self) -> None:
        for server in self.servers.values():
            server.close()
        self.servers.clear()

    def __enter__(self) -> "MCPConnector":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

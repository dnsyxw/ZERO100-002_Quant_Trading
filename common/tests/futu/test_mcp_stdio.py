"""MCP stdio 协议框架的单元测试(不依赖富途/OpenD)。"""
from __future__ import annotations

import io
import json

import pytest

from mcp.mcp_stdio import (
    DEFAULT_PROTOCOL_VERSION,
    MCPServer,
    ToolFailure,
    _missing_required,
    _tool_error,
    _tool_ok,
)


def make_server() -> MCPServer:
    server = MCPServer("demo", "1.2.3", "说明文字")

    def echo(args):
        return {"got": args.get("text"), "n": len(args.get("text") or "")}

    def boom(args):
        raise RuntimeError("内部炸了")

    def refuse(args):
        raise ToolFailure("业务上不允许")

    server.add_tool("echo_text", "回显", {"type": "object", "properties": {"text": {"type": "string"}},
                                          "required": ["text"], "additionalProperties": False}, echo)
    server.add_tool("boom", "抛异常", {"type": "object", "properties": {}}, boom)
    server.add_tool("refuse", "主动拒绝", {"type": "object", "properties": {}}, refuse)
    return server


def request(server, method, params=None, msg_id=1):
    msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return server.handle_message(msg)


class TestInitialize:
    def test_回显受支持的协议版本(self):
        server = make_server()
        for version in ("2025-11-25", "2025-06-18", "2024-11-05"):
            result = request(server, "initialize", {"protocolVersion": version})["result"]
            assert result["protocolVersion"] == version

    def test_未知版本回退默认(self):
        server = make_server()
        result = request(server, "initialize", {"protocolVersion": "1999-01-01"})["result"]
        assert result["protocolVersion"] == DEFAULT_PROTOCOL_VERSION

    def test_声明工具能力与说明(self):
        server = make_server()
        result = request(server, "initialize", {})["result"]
        assert result["capabilities"]["tools"]["listChanged"] is False
        assert result["serverInfo"] == {"name": "demo", "version": "1.2.3"}
        assert result["instructions"] == "说明文字"

    def test_无说明时不带instructions(self):
        result = request(MCPServer("bare"), "initialize", {})["result"]
        assert "instructions" not in result


class TestToolList:
    def test_按注册顺序返回(self):
        tools = request(make_server(), "tools/list", {})["result"]["tools"]
        assert [t["name"] for t in tools] == ["echo_text", "boom", "refuse"]
        assert tools[0]["inputSchema"]["required"] == ["text"]

    def test_重复注册报错(self):
        server = MCPServer("x")
        server.add_tool("a", "", {"type": "object"}, lambda a: {})
        with pytest.raises(ValueError, match="重复"):
            server.add_tool("a", "", {"type": "object"}, lambda a: {})


class TestToolCall:
    def test_成功返回文本与结构化内容(self):
        result = request(make_server(), "tools/call", {"name": "echo_text", "arguments": {"text": "hi"}})["result"]
        assert result["isError"] is False
        assert result["structuredContent"] == {"got": "hi", "n": 2}
        assert json.loads(result["content"][0]["text"])["got"] == "hi"

    def test_缺参返回_isError(self):
        result = request(make_server(), "tools/call", {"name": "echo_text", "arguments": {}})["result"]
        assert result["isError"] is True
        assert "text" in result["content"][0]["text"]

    def test_业务拒绝返回_isError(self):
        result = request(make_server(), "tools/call", {"name": "refuse", "arguments": {}})["result"]
        assert result["isError"] is True
        assert "业务上不允许" in result["content"][0]["text"]

    def test_未捕获异常也返回_isError_而非崩服务(self):
        result = request(make_server(), "tools/call", {"name": "boom", "arguments": {}})["result"]
        assert result["isError"] is True
        assert "内部炸了" in result["content"][0]["text"]

    def test_未知工具是协议错误(self):
        msg = request(make_server(), "tools/call", {"name": "nope", "arguments": {}})
        assert "error" in msg and msg["error"]["code"] == -32602

    def test_arguments_非对象时按空处理(self):
        result = request(make_server(), "tools/call", {"name": "boom", "arguments": "乱传"})["result"]
        assert result["isError"] is True


class TestNotificationsAndErrors:
    def test_通知不返回响应(self):
        server = make_server()
        assert server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
        assert server.handle_message({"jsonrpc": "2.0", "method": "notifications/cancelled"}) is None

    def test_ping(self):
        assert request(make_server(), "ping")["result"] == {}

    def test_未知方法(self):
        msg = request(make_server(), "resources/list")
        assert msg["error"]["code"] == -32601

    def test_非JSONRPC报文(self):
        msg = make_server().handle_message({"id": 1, "method": "ping"})
        assert msg["error"]["code"] == -32600

    def test_非对象报文(self):
        msg = make_server().handle_message([1, 2, 3])
        assert msg["error"]["code"] == -32600


class TestStdioLoop:
    def test_换行分隔收发(self):
        server = make_server()
        stdin = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
            + "\n"  # 空行应被跳过
            + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n"
        )
        stdout = io.StringIO()
        assert server.serve(stdin=stdin, stdout=stdout) == 0
        lines = [json.loads(l) for l in stdout.getvalue().strip().splitlines()]
        assert [m.get("id") for m in lines] == [1, 2]

    def test_非法JSON回错误而不是崩溃(self):
        server = make_server()
        stdin = io.StringIO("{ 这不是 json }\n")
        stdout = io.StringIO()
        server.serve(stdin=stdin, stdout=stdout)
        msg = json.loads(stdout.getvalue().strip())
        assert msg["error"]["code"] == -32700
        assert msg["id"] is None


class TestHelpers:
    def test_missing_required(self):
        schema = {"type": "object", "required": ["a", "b"], "properties": {"a": {}, "b": {}}}
        assert _missing_required(schema, {"a": 1}) == ["b"]
        assert _missing_required(schema, {"a": 1, "b": None}) == ["b"]
        assert _missing_required(schema, {"a": 1, "b": 2}) == []

    def test_tool_ok_字符串直接作为文本(self):
        assert _tool_ok("纯文本")["content"][0]["text"] == "纯文本"
        assert "structuredContent" not in _tool_ok("纯文本")

    def test_tool_error(self):
        payload = _tool_error("坏了")
        assert payload["isError"] is True and payload["content"][0]["text"] == "坏了"


class TestStrictJson:
    """Python 的 json 默认会写出 NaN/Infinity —— 那是非法 JSON, 客户端 JSON.parse 会炸。"""

    def test_NaN_与_Inf_变成_null(self):
        result = _tool_ok({"a": float("nan"), "b": float("inf"), "c": [float("-inf")]})
        assert result["structuredContent"] == {"a": None, "b": None, "c": [None]}
        assert "NaN" not in result["content"][0]["text"]
        assert "Infinity" not in result["content"][0]["text"]

    def test_numpy_标量转原生(self):
        import numpy as np

        result = _tool_ok({"i": np.int64(3), "f": np.float64(1.5), "b": np.bool_(True)})
        assert result["structuredContent"] == {"i": 3, "f": 1.5, "b": True}
        json.loads(result["content"][0]["text"])

    def test_不可序列化对象转字符串(self):
        class Weird:
            def __str__(self):
                return "weird"

        result = _tool_ok({"obj": Weird()})
        assert result["structuredContent"] == {"obj": "weird"}

    def test_写出的一行是严格JSON(self):
        server = MCPServer("x")
        server.add_tool("nan_tool", "", {"type": "object", "properties": {}}, lambda a: {"v": float("nan")})
        stdin = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": "nan_tool", "arguments": {}}}) + "\n")
        stdout = io.StringIO()
        server.serve(stdin=stdin, stdout=stdout)
        raw = stdout.getvalue().strip()
        assert "NaN" not in raw
        json.loads(raw, parse_constant=lambda name: pytest.fail(f"出现非法 JSON 常量: {name}"))

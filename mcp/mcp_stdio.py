"""极简 MCP (Model Context Protocol) stdio 服务端框架 —— 零第三方依赖。

只实现本项目的 MCP 客户端实际使用的那部分协议:

- 传输: stdio, **换行分隔的 JSON-RPC 2.0**(与官方 SDK `StdioClientTransport` 一致)
- 方法: `initialize` / `notifications/initialized` / `ping` / `tools/list` / `tools/call`
- 能力声明: `tools.listChanged = false`(工具集在进程生命周期内固定)

**stdout 纪律**: stdout 只能出现 JSON-RPC 报文。所有日志一律走 stderr;
富途 SDK 自己的控制台日志也已由 `quant_common.futu._bootstrap` 重定向到 stderr。
"""
from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

__all__ = ["ToolSpec", "MCPServer", "ToolFailure", "jsonable"]

#: 支持并回显的协议版本(与 @modelcontextprotocol/sdk 1.29 的集合取交集)
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05", "2024-10-07")
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

# JSON-RPC 错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ToolFailure(Exception):
    """工具层主动返回的"业务失败": 以 `isError=true` 返回给模型, 而不是协议错误。

    用于"调用方参数不对/前置条件不满足"这类 **模型应当看到并自行纠正** 的情况,
    例如 OpenD 未启动、标的不存在、被风控拒绝。
    """


def _stderr(message: str) -> None:
    print(f"[futu-mcp] {message}", file=sys.stderr, flush=True)


@dataclass
class ToolSpec:
    """一个 MCP 工具的完整定义。"""

    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], Any]
    tags: tuple[str, ...] = field(default_factory=tuple)


class MCPServer:
    """stdio MCP 服务端。

    Args:
        name: `serverInfo.name`(对模型展示的服务名)。
        version: `serverInfo.version`。
        instructions: 可选的 `initialize.instructions`(给模型的整体说明)。
    """

    def __init__(self, name: str, version: str = "0.1.0", instructions: str | None = None):
        self.name = name
        self.version = version
        self.instructions = instructions
        self._tools: dict[str, ToolSpec] = {}
        self._order: list[str] = []
        self.protocol_version = DEFAULT_PROTOCOL_VERSION

    # ------------------------------------------------------------------ 注册
    def add_tool(self, name: str, description: str, input_schema: dict, handler: Callable[[dict], Any],
                 tags: Iterable[str] = ()) -> None:
        """注册一个工具(重复名字直接报错, 避免静默覆盖)。"""
        if name in self._tools:
            raise ValueError(f"工具名重复: {name}")
        self._tools[name] = ToolSpec(name, description, input_schema, handler, tuple(tags))
        self._order.append(name)

    def tool(self, name: str, description: str, input_schema: dict | None = None, tags: Iterable[str] = ()):
        """装饰器形式的注册。"""
        def decorate(fn: Callable[[dict], Any]):
            self.add_tool(name, description, input_schema or _schema_from_signature(fn), fn, tags)
            return fn

        return decorate

    @property
    def tools(self) -> list[ToolSpec]:
        return [self._tools[n] for n in self._order]

    # ------------------------------------------------------------------ 协议
    def handle_message(self, message: Any) -> dict | None:
        """处理一条报文; 返回要写回 stdout 的响应, 通知类返回 None。"""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error_response(message.get("id") if isinstance(message, dict) else None,
                                   INVALID_REQUEST, "非法 JSON-RPC 报文")
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params") or {}
        is_notification = "id" not in message

        try:
            if method == "initialize":
                result = self._initialize(params)
            elif method in ("notifications/initialized", "initialized"):
                return None
            elif method == "notifications/cancelled" or method == "notifications/progress":
                return None
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = self._list_tools(params)
            elif method == "tools/call":
                unknown = (params or {}).get("name")
                if unknown not in self._tools:
                    if is_notification:
                        return None
                    return _error_response(msg_id, INVALID_PARAMS, f"未知工具: {unknown}")
                result = self._call_tool(params)
            else:
                if is_notification:
                    return None
                return _error_response(msg_id, METHOD_NOT_FOUND, f"不支持的方法: {method}")
        except Exception as exc:  # pragma: no cover - 兜底, 绝不让服务挂掉
            _stderr(f"处理 {method} 异常: {exc}\n{traceback.format_exc()}")
            if is_notification:
                return None
            return _error_response(msg_id, INTERNAL_ERROR, f"服务端异常: {exc}")

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        self.protocol_version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
        result = {
            "protocolVersion": self.protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.name, "version": self.version},
        }
        if self.instructions:
            result["instructions"] = self.instructions
        return result

    def _list_tools(self, params: dict) -> dict:
        return {
            "tools": [
                {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
                for t in self.tools
            ]
        }

    def _call_tool(self, params: dict) -> dict:
        name = params.get("name")
        args = params.get("arguments") or {}
        spec = self._tools.get(name)
        if spec is None:
            raise ToolFailure(f"未知工具: {name}")
        if not isinstance(args, dict):
            args = {}
        missing = _missing_required(spec.input_schema, args)
        if missing:
            return _tool_error(
                f"参数缺失: {', '.join(missing)}。请按 inputSchema 补齐后重试。"
                f"\n需要的参数: {json.dumps(spec.input_schema.get('properties', {}), ensure_ascii=False)}"
            )
        try:
            payload = spec.handler(args)
        except ToolFailure as exc:
            return _tool_error(str(exc))
        except Exception as exc:
            _stderr(f"工具 {name} 执行失败: {exc}\n{traceback.format_exc()}")
            return _tool_error(f"{type(exc).__name__}: {exc}")
        return _tool_ok(payload)

    # ------------------------------------------------------------------ 事件循环
    def serve(self, stdin=None, stdout=None) -> int:
        """阻塞式 stdio 循环, 直到 stdin EOF。"""
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        _stderr(f"{self.name} {self.version} 就绪, 共 {len(self.tools)} 个工具")
        for raw in stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                _write(stdout, _error_response(None, PARSE_ERROR, f"JSON 解析失败: {exc}"))
                continue
            response = self.handle_message(message)
            if response is not None:
                _write(stdout, response)
        _stderr("stdin 关闭, 退出")
        return 0


# ---------------------------------------------------------------------- 辅助
def _write(stdout, payload: dict) -> None:
    stdout.write(json.dumps(_jsonable(payload), ensure_ascii=False) + "\n")
    stdout.flush()


def _jsonable(value: Any) -> Any:
    """递归清洗成**严格 JSON** 安全的值。

    为什么必须做: Python 的 `json.dumps` 默认会把 NaN/Infinity 原样写成 `NaN`/`Infinity` ——
    这是非法 JSON, 客户端的 `JSON.parse` 会直接抛错。数据来自 pandas/numpy, NaN 很常见。
    顺带把 numpy 标量、Timestamp 等转成原生类型。
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return None if (value != value or value in (float("inf"), float("-inf"))) else value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "item"):  # numpy 标量
        try:
            return _jsonable(value.item())
        except Exception:  # pragma: no cover
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:  # pragma: no cover
            pass
    return str(value)


def _error_response(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _tool_ok(payload: Any) -> dict:
    """成功结果: text 给模型看, structuredContent 给程序用。"""
    clean = _jsonable(payload)
    text = clean if isinstance(clean, str) else json.dumps(clean, ensure_ascii=False, indent=2)
    result: dict = {"content": [{"type": "text", "text": text}], "isError": False}
    if not isinstance(clean, str):
        result["structuredContent"] = clean
    return result


def _tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _missing_required(schema: dict, args: dict) -> list[str]:
    required = schema.get("required") or []
    return [key for key in required if args.get(key) is None]


#: 供调用方复用的严格 JSON 清洗函数(见 `_jsonable`)
jsonable = _jsonable


def _schema_from_signature(fn: Callable) -> dict:
    """无 schema 时的兜底: 允许任意对象。"""
    return {"type": "object", "properties": {}, "additionalProperties": True}

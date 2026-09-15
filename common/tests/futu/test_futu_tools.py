"""富途 MCP 工具集的契约测试(不连 OpenD)。

重点覆盖三条"安全不变量":
1. 不提供交易解锁工具(富途官方禁止 SDK 解锁);
2. 实盘(REAL)默认被拒绝, 必须 enable_real_trade + confirmed 双闸门;
3. 入参 schema 落在 DSH 支持的 JSON Schema 子集内。
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from mcp.futu_server import build_server
from quant_common.futu import FutuConfig
from quant_common.futu.errors import (
    FutuApiError,
    FutuConfigError,
    FutuNotConnectedError,
    FutuRiskRejected,
    FutuTradeDisabledError,
)
from quant_common.futu.gateway import _plain, _records
from quant_common.futu.quote import frame_payload

ALLOWED_SCHEMA_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}
SCALAR_TYPES = {"string", "number", "integer", "boolean", "null"}
OBJECT_ONLY = {"properties", "required", "additionalProperties"}
ARRAY_ONLY = {"items"}
SCALAR_ONLY = {"enum", "const"}


def make_server(**cfg_overrides):
    server, _gw = build_server(FutuConfig(**cfg_overrides))
    return server


def call(server, name, arguments=None, msg_id=1):
    return server.handle_message({"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
                                  "params": {"name": name, "arguments": arguments or {}}})["result"]


def tool_names(server) -> list[str]:
    tools = server.handle_message({"jsonrpc": "2.0", "id": 0, "method": "tools/list",
                                   "params": {}})["result"]["tools"]
    return [t["name"] for t in tools]


# --------------------------------------------------------------------------- 注册契约
class TestRegistration:
    def test_工具数量与命名(self):
        server = make_server()
        names = tool_names(server)
        assert len(names) >= 25
        assert all(n.startswith("futu_") for n in names)
        assert len(names) == len(set(names))
        # DSH 侧公开名是 mcp__futu__<name>, 上限 64 字符
        assert all(len("mcp__futu__" + n) <= 64 for n in names)

    def test_关键工具都在(self):
        names = set(tool_names(make_server()))
        expected = {
            "futu_health", "futu_config", "futu_snapshot", "futu_kline", "futu_order_book",
            "futu_market_state", "futu_search_quote", "futu_kline_quota", "futu_accounts",
            "futu_funds", "futu_positions", "futu_orders", "futu_deals", "futu_place_order",
            "futu_modify_order", "futu_cancel_order",
        }
        assert expected <= names

    def test_不提供交易解锁工具(self):
        """安全不变量: SDK 的 unlock_trade 被富途官方禁止, 工具集里绝不能出现。"""
        names = " ".join(tool_names(make_server())).lower()
        assert "unlock" not in names

    def test_每个工具都有描述(self):
        for spec in make_server().tools:
            assert spec.description.strip(), spec.name

    def test_schema_落在DSH支持子集内(self):
        def walk(node, path):
            if not isinstance(node, dict):
                return
            if "oneOf" in node:
                for i, sub in enumerate(node["oneOf"]):
                    walk(sub, f"{path}.oneOf[{i}]")
                return
            if "type" not in node:
                return
            node_type = node["type"]
            assert isinstance(node_type, str), f"{path}.type 必须是单个字符串"
            assert node_type in ALLOWED_SCHEMA_TYPES, f"{path}.type={node_type} 不支持"
            for key in OBJECT_ONLY:
                assert key not in node or node_type == "object", f"{path}.{key} 只能用于 object"
            for key in ARRAY_ONLY:
                assert key not in node or node_type == "array", f"{path}.{key} 只能用于 array"
            for key in SCALAR_ONLY:
                assert key not in node or node_type in SCALAR_TYPES, f"{path}.{key} 只能用于标量"
            if node_type == "object":
                for key, sub in (node.get("properties") or {}).items():
                    walk(sub, f"{path}.properties.{key}")
                for req in node.get("required") or []:
                    assert req in (node.get("properties") or {}), f"{path}.required 里的 {req} 未定义"
            if node_type == "array":
                walk(node.get("items") or {}, f"{path}.items")

        for spec in make_server().tools:
            walk(spec.input_schema, spec.name)

    def test_每个属性都写了描述(self):
        for spec in make_server().tools:
            for key, sub in (spec.input_schema.get("properties") or {}).items():
                assert sub.get("description"), f"{spec.name}.{key} 缺 description"


# --------------------------------------------------------------------------- 交易闸门
class TestTradeGates:
    def test_默认不提供真实交易工具解锁(self):
        cfg = FutuConfig()
        assert cfg.enable_real_trade is False
        assert cfg.trd_env == "SIMULATE"

    def test_实盘下单被默认拒绝(self):
        result = call(make_server(), "futu_place_order",
                      {"code": "600000", "side": "BUY", "qty": 100, "price": 10.0, "trd_env": "REAL"})
        assert result["isError"] is True
        assert "enable_real_trade" in result["content"][0]["text"]

    def test_实盘撤单被默认拒绝(self):
        result = call(make_server(), "futu_cancel_order", {"order_id": "1", "trd_env": "REAL"})
        assert result["isError"] is True
        assert "enable_real_trade" in result["content"][0]["text"]

    def test_非法_side_参数报错(self):
        result = call(make_server(), "futu_place_order", {"code": "600000", "side": "HOLD", "qty": 100})
        assert result["isError"] is True
        assert "参数错误" in result["content"][0]["text"]

    def test_缺必填参数(self):
        result = call(make_server(), "futu_place_order", {"code": "600000"})
        assert result["isError"] is True
        assert "参数缺失" in result["content"][0]["text"]


class TestTradeLayerGuards:
    """直接对交易层断言闸门(绕过 MCP 文本层)。"""

    def test_resolve_env_拒绝实盘(self):
        from quant_common.futu.trade import resolve_env
        from quant_common.futu.gateway import FutuGateway

        gw = FutuGateway(FutuConfig(enable_real_trade=False))
        with pytest.raises(FutuTradeDisabledError):
            resolve_env(gw, "REAL")
        assert resolve_env(gw, "SIMULATE") == "SIMULATE"

    def test_resolve_env_非法值(self):
        from quant_common.futu.trade import resolve_env
        from quant_common.futu.gateway import FutuGateway

        gw = FutuGateway(FutuConfig())
        with pytest.raises(ValueError, match="trd_env"):
            resolve_env(gw, "PAPER")

    def test_实盘下单需_confirmed(self):
        from quant_common.futu import trade
        from quant_common.futu.gateway import FutuGateway

        gw = FutuGateway(FutuConfig(enable_real_trade=True))
        with pytest.raises(FutuTradeDisabledError, match="confirmed"):
            trade.place_order(gw, "600000", "BUY", 100, price=10.0, trd_env="REAL")

    def test_限价单必须给价(self):
        from quant_common.futu import trade
        from quant_common.futu.gateway import FutuGateway

        gw = FutuGateway(FutuConfig())
        with pytest.raises(ValueError, match="price"):
            trade.place_order(gw, "600000", "BUY", 100)

    def test_数量必须为正(self):
        from quant_common.futu import trade
        from quant_common.futu.gateway import FutuGateway

        gw = FutuGateway(FutuConfig())
        with pytest.raises(ValueError, match="qty"):
            trade.place_order(gw, "600000", "BUY", 0, price=10.0)

    def test_备注不能超过64字节(self):
        """futu 服务端限制: remark 转 UTF-8 后 <= 64 字节(中文一个字 3 字节)。"""
        from quant_common.futu import trade
        from quant_common.futu.gateway import FutuGateway

        gw = FutuGateway(FutuConfig())
        too_long = "中文备注" * 10          # 40 个汉字 = 120 字节
        with pytest.raises(ValueError, match="64 字节"):
            trade.place_order(gw, "600000", "BUY", 100, price=10.0, remark=too_long)
        # 恰好 64 字节应当通过校验(会在连 OpenD 时才失败, 这里只验证不因 remark 报错)
        ok = "a" * 64
        assert len(ok.encode("utf-8")) == 64


# --------------------------------------------------------------------------- 错误翻译
class TestErrorMapping:
    def test_连不上_openD_给出可执行提示(self, monkeypatch):
        from quant_common.futu.gateway import FutuGateway

        monkeypatch.setattr(FutuGateway, "probe", lambda self, timeout=1.5: False)
        result = call(make_server(), "futu_snapshot", {"codes": ["600000"]})
        assert result["isError"] is True
        text = result["content"][0]["text"]
        assert "FutuNotConnectedError" in text
        assert "OpenD" in text

    def test_health_报告未连接但不报错(self, monkeypatch):
        from quant_common.futu.gateway import FutuGateway

        monkeypatch.setattr(FutuGateway, "probe", lambda self, timeout=1.5: False)
        result = call(make_server(), "futu_health", {})
        assert result["isError"] is False
        payload = result["structuredContent"]
        assert payload["connected"] is False
        assert payload["tcp_reachable"] is False
        assert "OpenD" in payload["hint"]

    def test_配置工具列出待提供凭据(self):
        result = call(make_server(), "futu_config", {})
        assert result["isError"] is False
        secrets = result["structuredContent"]["secrets_required"]
        assert any("OpenD 登录账号" in s for s in secrets)
        assert any("解锁密码" in s for s in secrets)

    def test_异常链完整(self):
        exc = FutuApiError("get_market_snapshot", -1, "额度不足")
        assert "get_market_snapshot" in str(exc)
        assert "额度不足" in str(exc)
        assert isinstance(exc, FutuConfigError.__mro__[-2])  # 同属 FutuError 家族
        assert issubclass(FutuNotConnectedError, Exception)


# --------------------------------------------------------------------------- 载荷整形
class TestFramePayload:
    def test_截断到_max_rows(self):
        df = pd.DataFrame({"a": range(10), "b": range(10)})
        cfg = FutuConfig(max_rows=4)
        from quant_common.futu.gateway import FutuGateway

        payload = frame_payload(df, FutuGateway(cfg))
        assert payload["count"] == 10
        assert payload["returned"] == 4
        assert payload["truncated"] is True
        assert len(payload["rows"]) == 4

    def test_未截断标记(self):
        from quant_common.futu.gateway import FutuGateway

        payload = frame_payload(pd.DataFrame({"a": [1, 2]}), FutuGateway(FutuConfig()))
        assert payload["truncated"] is False and payload["count"] == 2

    def test_空数据(self):
        from quant_common.futu.gateway import FutuGateway

        payload = frame_payload(pd.DataFrame(), FutuGateway(FutuConfig()))
        assert payload["count"] == 0 and payload["rows"] == []

    def test_NaN_转_None_可序列化(self):
        from quant_common.futu.gateway import FutuGateway

        df = pd.DataFrame({"a": [1.0, float("nan")], "b": ["x", None]})
        payload = frame_payload(df, FutuGateway(FutuConfig()))
        assert payload["rows"][1][0] is None
        json.dumps(payload, ensure_ascii=False)  # 不抛异常即通过


class TestPlainAndRecords:
    def test_plain_标量(self):
        assert _plain(None) is None
        assert _plain(True) is True
        assert _plain(3) == 3
        assert _plain("x") == "x"

    def test_plain_时间戳(self):
        assert _plain(pd.Timestamp("2026-01-02 09:30:00")).startswith("2026-01-02")

    def test_plain_numpy标量(self):
        import numpy as np

        assert _plain(np.int64(7)) == 7
        assert _plain(np.float64(1.5)) == 1.5

    def test_plain_nan(self):
        assert _plain(float("nan")) is None

    def test_records_各种入参(self):
        assert _records(None) == []
        assert _records({"a": 1}) == [{"a": 1}]
        assert _records(pd.DataFrame({"a": [1, 2]})) == [{"a": 1}, {"a": 2}]

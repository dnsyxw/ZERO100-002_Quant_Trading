"""端到端: 以真实子进程方式拉起 MCP 服务, 走完整 stdio 握手。

沙箱/受限环境不允许父进程用管道捕获子进程输出, 因此这里用**临时文件**当
stdin/stdout —— 协议本身不变(换行分隔 JSON-RPC), 同时验证了"stdout 只有报文"。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

#: common/tests/futu/test_stdio_e2e.py -> [0]=futu, [1]=tests, [2]=common, [3]=仓库根
ROOT = Path(__file__).resolve().parents[3]
SERVER = ROOT / "mcp" / "futu_server.py"


def run_server(requests: list[dict], timeout: int = 120, extra_conf: dict | None = None) -> list[dict]:
    """把 requests 写进文件当 stdin, 返回服务端的全部响应。

    Args:
        extra_conf: 需要覆盖配置时传入(写成临时 config 并用 --config 传给服务)。
            用来让测试**不依赖本机是否真的开着 OpenD** —— 例如把 port 指到没人监听的
            端口, 就得到确定的"连不上"分支。
    """
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="futu_mcp_e2e_"))
    inp, outp, errp = tmp / "in.jsonl", tmp / "out.jsonl", tmp / "err.txt"
    inp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in requests) + "\n", encoding="utf-8")

    argv = [sys.executable, str(SERVER)]
    if extra_conf is not None:
        cfg_path = tmp / "futu.json"
        cfg_path.write_text(json.dumps(extra_conf, ensure_ascii=False), encoding="utf-8")
        argv += ["--config", str(cfg_path)]

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    with inp.open("r", encoding="utf-8") as fi, outp.open("w", encoding="utf-8") as fo, \
            errp.open("w", encoding="utf-8") as fe:
        proc = subprocess.Popen(argv, stdin=fi, stdout=fo, stderr=fe, cwd=str(ROOT), env=env)
        code = proc.wait(timeout=timeout)
    assert code == 0, f"服务退出码 {code}; stderr={errp.read_text(encoding='utf-8')[-2000:]}"

    raw = outp.read_text(encoding="utf-8")
    lines = [l for l in raw.splitlines() if l.strip()]
    responses = []
    for line in lines:
        responses.append(json.loads(line))  # 每行都必须是完整 JSON, 否则说明 stdout 被污染
    return responses


#: 指向一个**必然没人监听**的端口, 让"连不上 OpenD"分支可确定复现,
#: 不受开发机当前是否开着 OpenD 影响。
DEAD_PORT_CONF = {"port": 1, "log_dir": "runtime/futu_appdata",
                  "order_log": "runtime/futu_orders.jsonl"}


def by_id(responses: list[dict], msg_id: int) -> dict:
    for msg in responses:
        if msg.get("id") == msg_id:
            return msg
    raise AssertionError(f"没有 id={msg_id} 的响应: {responses}")


class TestStdioEndToEnd:
    def test_握手与工具发现(self):
        responses = run_server([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "pytest", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ])
        assert len(responses) == 2  # 通知不产生响应

        init = by_id(responses, 1)["result"]
        assert init["protocolVersion"] == "2025-06-18"
        assert init["serverInfo"]["name"] == "futu"
        assert init["capabilities"]["tools"]["listChanged"] is False

        tools = by_id(responses, 2)["result"]["tools"]
        assert len(tools) >= 25
        assert all("inputSchema" in t for t in tools)

    def test_stdout_不含非协议输出(self):
        responses = run_server([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "futu_health", "arguments": {}}},
        ])
        assert all(m.get("jsonrpc") == "2.0" for m in responses)
        # 富途 SDK 的控制台日志必须走 stderr, 不能混进 stdout
        assert all("FTConsoleLog" not in json.dumps(m, ensure_ascii=False) for m in responses)

    def test_health_在无_openD_时给出提示而非崩溃(self):
        # 把 port 指到必然没人监听的地方 —— 这样无论开发机是否开着 OpenD, 结果都确定
        responses = run_server([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "futu_health", "arguments": {}}},
        ], extra_conf=DEAD_PORT_CONF)
        payload = by_id(responses, 2)["result"]
        assert payload["isError"] is False
        assert payload["structuredContent"]["connected"] is False
        assert payload["structuredContent"]["tcp_reachable"] is False
        assert "OpenD" in payload["structuredContent"]["hint"]

    def test_真连上_openD_时_health_报告已连接(self):
        """开发机开着 OpenD 时, 这条会走"已连接"分支; 没开则跳过。"""
        import socket

        try:
            with socket.create_connection(("127.0.0.1", 11111), timeout=2):
                pass
        except OSError:
            import pytest
            pytest.skip("本机没有运行 OpenD, 跳过")
        responses = run_server([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "futu_health", "arguments": {}}},
        ])
        payload = by_id(responses, 2)["result"]
        assert payload["isError"] is False
        assert payload["structuredContent"]["connected"] is True
        assert payload["structuredContent"]["opend"]["qot_logined"] is True

    def test_实盘闸门在子进程里同样生效(self):
        responses = run_server([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "futu_place_order",
                        "arguments": {"code": "600000", "side": "BUY", "qty": 100,
                                      "price": 10, "trd_env": "REAL"}}},
        ])
        payload = by_id(responses, 2)["result"]
        assert payload["isError"] is True
        assert "enable_real_trade" in payload["content"][0]["text"]


def test_selftest_子命令通过():
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    import tempfile

    errp = Path(tempfile.mkdtemp(prefix="futu_selftest_")) / "err.txt"
    with errp.open("w", encoding="utf-8") as fe:
        code = subprocess.call([sys.executable, str(SERVER), "--selftest"],
                               cwd=str(ROOT), env=env, stdout=fe, stderr=fe)
    assert code == 0, errp.read_text(encoding="utf-8")[-2000:]


def test_list_tools_子命令可用():
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    import tempfile

    errp = Path(tempfile.mkdtemp(prefix="futu_listtools_")) / "err.txt"
    with errp.open("w", encoding="utf-8") as fe:
        code = subprocess.call([sys.executable, str(SERVER), "--list-tools"],
                               cwd=str(ROOT), env=env, stdout=fe, stderr=fe)
    text = errp.read_text(encoding="utf-8")
    assert code == 0
    assert "futu_snapshot" in text and "共 " in text

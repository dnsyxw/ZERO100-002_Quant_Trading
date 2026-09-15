"""`FutuGateway` 连接管理的并发回归测试(不连 OpenD)。

这里守着一个实测踩过的**死锁**:
`trade_ctx` 在主线程持 `self._lock` 的情况下 `join()` 后台构造线程, 而后台线程
要拿同一把锁才能发布 `_trade_ctx` —— 于是必然等到超时才失败; 锁一释放后台线程
立刻完成, 表现为"第一次交易调用必超时, 第二次就好了"。

修复: `join()` 移到锁外。下面的测试会在几百毫秒内断言通过 —— 若死锁回归,
它会等到 TRADE_CONNECT_TIMEOUT(20s) 才失败。
"""
from __future__ import annotations

import threading
import time

import pytest

from quant_common.futu.config import FutuConfig
from quant_common.futu.errors import FutuNotConnectedError
from quant_common.futu.gateway import TRADE_CONNECT_TIMEOUT, FutuGateway


@pytest.fixture
def fast_gateway(monkeypatch):
    """一个 probe 永远成功、后台构造很快完成的 gateway。"""
    gw = FutuGateway(FutuConfig())
    monkeypatch.setattr(FutuGateway, "probe", lambda self, timeout=1.5: True)

    def fake_build(self, key):
        time.sleep(0.05)  # 模拟真实的建连耗时
        with self._lock:  # ← 死锁复现点: 后台线程必须能拿到这把锁
            self._trade_ctxs[key] = f"ctx-{key}"

    monkeypatch.setattr(FutuGateway, "_build_trade_ctx", fake_build)
    return gw


class TestTradeContextNoDeadlock:
    def test_首次调用不会等到超时(self, fast_gateway):
        started = time.time()
        ctx = fast_gateway.trade_ctx
        elapsed = time.time() - started
        assert ctx == "ctx-CN"          # 配置默认市场 CN
        assert elapsed < 2.0, (
            f"取 trade_ctx 花了 {elapsed:.1f}s —— 疑似重新引入了持锁 join 死锁"
            f"(超时阈值 {TRADE_CONNECT_TIMEOUT}s)"
        )

    def test_并发调用同一上下文(self, fast_gateway):
        got: list[object] = []
        errors: list[BaseException] = []

        def worker():
            try:
                got.append(fast_gateway.trade_ctx)
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert not errors, errors
        assert len(got) == 8
        assert len({id(c) for c in got}) == 1, "并发下应复用同一个上下文"

    def test_构造失败时给出可读错误而不是卡死(self, monkeypatch):
        gw = FutuGateway(FutuConfig())
        monkeypatch.setattr(FutuGateway, "probe", lambda self, timeout=1.5: True)
        monkeypatch.setattr(FutuGateway, "_build_trade_ctx", lambda self, key: None)  # 永不发布
        monkeypatch.setattr("quant_common.futu.gateway.TRADE_CONNECT_TIMEOUT", 0.3)
        started = time.time()
        with pytest.raises(FutuNotConnectedError, match="交易上下文"):
            _ = gw.trade_ctx
        assert time.time() - started < 5.0

    def test_端口不通时直接报离线提示(self, monkeypatch):
        gw = FutuGateway(FutuConfig())
        monkeypatch.setattr(FutuGateway, "probe", lambda self, timeout=1.5: False)
        with pytest.raises(FutuNotConnectedError, match="OpenD"):
            _ = gw.trade_ctx


class TestPerMarketTradeContext:
    """A股/港股/美股要各用一个交易上下文 —— 富途的资金账户是按市场分的。"""

    def test_不同市场各建一个上下文(self, fast_gateway):
        assert fast_gateway.trade_ctx_for("CN") == "ctx-CN"
        assert fast_gateway.trade_ctx_for("HK") == "ctx-HK"
        assert fast_gateway.trade_ctx_for("US") == "ctx-US"
        assert set(fast_gateway._trade_ctxs) == {"CN", "HK", "US"}

    def test_市场别名归一到同一上下文(self, fast_gateway):
        assert fast_gateway.trade_ctx_for("SH") == fast_gateway.trade_ctx_for("CN")
        assert fast_gateway.trade_ctx_for("SZ") == fast_gateway.trade_ctx_for("CN")
        assert fast_gateway.trade_ctx_for("hk") == fast_gateway.trade_ctx_for("HK")

    def test_市场间互不影响(self, fast_gateway):
        fast_gateway.trade_ctx_for("HK")
        assert "CN" not in fast_gateway._trade_ctxs  # 港股建连不应顺带建 A 股


class TestResolveAccId:
    """账户选择: acc_id 覆盖 > acc_ids[市场] > 自动挑选。"""

    def test_按市场取固定账户(self):
        cfg = FutuConfig(acc_ids={"CN": 111, "HK": 222, "US": 333})
        gw = FutuGateway(cfg)
        assert gw.resolve_acc_id("CN")[0] == 111
        assert gw.resolve_acc_id("HK")[0] == 222
        assert gw.resolve_acc_id("US")[0] == 333
        # SH/SZ 归到 CN
        assert gw.resolve_acc_id("SH")[0] == 111
        assert gw.resolve_acc_id("SZ")[0] == 111

    def test_acc_id_覆盖_市场映射(self):
        cfg = FutuConfig(acc_id=999, acc_ids={"CN": 111, "HK": 222})
        gw = FutuGateway(cfg)
        assert gw.resolve_acc_id("HK")[0] == 999

    def test_未配置的市场不误用其它市场账户(self, monkeypatch):
        """HK 没配时应走自动挑选, 而不是回退去用 CN 的账户(那会下错账户)。"""
        cfg = FutuConfig(acc_ids={"CN": 111})
        gw = FutuGateway(cfg)
        seen: list[str] = []

        def fake_call_trade(api, *args, market=None, **kwargs):
            seen.append(market)
            import pandas as pd
            return pd.DataFrame([{"acc_id": 777, "acc_status": "ACTIVE"}])

        monkeypatch.setattr(FutuGateway, "call_trade", fake_call_trade)
        acc_id, info = gw.resolve_acc_id("HK")
        assert acc_id == 777
        assert seen == ["HK"], "应当只查 HK 市场的账户列表"


class TestQuoteContext:
    def test_端口不通时直接报离线提示(self, monkeypatch):
        gw = FutuGateway(FutuConfig())
        monkeypatch.setattr(FutuGateway, "probe", lambda self, timeout=1.5: False)
        with pytest.raises(FutuNotConnectedError, match="OpenD"):
            _ = gw.quote_ctx

    def test_close_可重复调用(self):
        gw = FutuGateway(FutuConfig())
        gw.close()
        gw.close()  # 不应抛异常
        assert gw._quote_ctx is None and gw._trade_ctxs == {}


class TestOfflineHint:
    def test_提示包含可执行步骤(self):
        gw = FutuGateway(FutuConfig(host="127.0.0.1", port=11111))
        hint = gw._offline_hint()
        assert "OpenD" in hint
        assert "11111" in hint
        assert "登录" in hint

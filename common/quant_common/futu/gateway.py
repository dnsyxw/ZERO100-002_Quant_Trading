"""OpenD 连接管理: 惰性建连、超时保护、线程安全、健康检查。

设计要点
--------
1. **绝不阻塞**: futu 的 `OpenContextBase.__init__` 在 `is_async_connect=False`(默认)时
   会 `while True` 每 6 秒重试连接 OpenD。若 OpenD 没启动, 构造 `OpenSecTradeContext`
   会**永久挂住**。因此:
   - 行情上下文用 `is_async_connect=True` 构造(立即返回);
   - 交易上下文没有异步开关, 放到后台守护线程里构造并限时等待, 超时即报
     `FutuNotConnectedError`, 后台线程继续尝试, 后续调用可自动接上。
2. **先探活再报错**: 连不上时先用 TCP 探一下端口, 给出"OpenD 未启动/未登录"的可执行提示,
   而不是把 SDK 的原始异常抛给模型。
3. **惰性**: 只有真正调用工具时才建连, 服务启动不依赖 OpenD。
"""
from __future__ import annotations

import socket
import threading
import time
from typing import Any

from ._bootstrap import prepare_process, sanitize_futu_logging
from .config import FutuConfig
from .errors import FutuApiError, FutuNotConnectedError

__all__ = ["FutuGateway", "RET_OK"]

RET_OK = 0

#: 交易上下文(无法异步构造)的最大等待时间, 秒。
#: 实测: OpenD 已运行时首次建连含交易登录握手, 可能明显超过 8s, 因此给到 20s。
TRADE_CONNECT_TIMEOUT = 20.0
#: 行情上下文异步连接后, 等待其就绪的时间, 秒
QUOTE_CONNECT_TIMEOUT = 8.0

_READY_STATUSES = ("CONNECTED", "READY")


class FutuGateway:
    """一个进程内共享的 OpenD 连接持有者。

    同一个 `host:port` 下 futu SDK 内部通过 `NetManager` 复用底层连接, 因此
    行情/交易上下文各自保持单例即可。
    """

    def __init__(self, cfg: FutuConfig):
        self.cfg = cfg
        self._lock = threading.RLock()
        self._quote_ctx = None
        # 交易上下文按市场各存一个: 富途的 OpenSecTradeContext 带 filter_trdmarket,
        # 一个上下文只覆盖一个市场, 而本项目要同时能操作 A股/港股/美股。
        self._trade_ctxs: dict[str, object] = {}
        self._trade_threads: dict[str, threading.Thread] = {}
        self._trade_errors: dict[str, BaseException] = {}
        self._futu = None

    # ------------------------------------------------------------------ 基础设施
    def sdk(self):
        """返回 futu SDK 模块(惰性导入并完成 import 期所需的进程准备)。"""
        return self._sdk()

    def _sdk(self):
        """惰性导入 futu SDK(第一次调用时完成 import 期所需的进程准备)。"""
        if self._futu is None:
            with self._lock:
                if self._futu is None:
                    prepare_process(app_data_dir=self.cfg.path(self.cfg.log_dir), isolate_stdout=False)
                    try:
                        import futu  # type: ignore
                    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
                        raise FutuNotConnectedError(
                            "未安装 futu-api SDK。请执行 `python scripts/check_futu_env.py --install` "
                            "或把 futu-api 装到仓库 pylibs/ 目录。"
                        ) from exc
                    sanitize_futu_logging()
                    self._futu = futu
        return self._futu

    def _security_firm(self):
        futu = self._sdk()
        name = str(self.cfg.security_firm or "FUTUSECURITIES").upper()
        return getattr(futu.SecurityFirm, name, futu.SecurityFirm.FUTUSECURITIES)

    def _trd_market(self, market: str | None = None):
        """市场键 -> futu `TrdMarket` 枚举。"""
        futu = self._sdk()
        name = self.cfg.market_key(market)
        return getattr(futu.TrdMarket, name, futu.TrdMarket.HK)

    def probe(self, timeout: float = 1.5) -> bool:
        """TCP 探活: OpenD 的 API 端口是否在监听。"""
        try:
            with socket.create_connection((self.cfg.host, self.cfg.port), timeout=timeout):
                return True
        except OSError:
            return False

    def _offline_hint(self) -> str:
        return (
            f"连不上 OpenD ({self.cfg.host}:{self.cfg.port})。"
            "请先人工启动并登录 Futu OpenD 桌面端(GUI 版): "
            "1) 下载 https://www.futunn.com/download/fetch-lasted-link?name=opend-windows ; "
            "2) 启动后用牛牛号/手机号/邮箱登录; "
            f"3) 确认监听地址为 {self.cfg.host}、API 端口为 {self.cfg.port}。"
            "本项目不会代为登录, 也不保存账号密码。"
        )

    # ------------------------------------------------------------------ 行情上下文
    @property
    def quote_ctx(self):
        """行情上下文(异步连接, 不会阻塞)。"""
        if self._quote_ctx is not None:
            return self._quote_ctx
        with self._lock:
            if self._quote_ctx is not None:
                return self._quote_ctx
            if not self.probe():
                raise FutuNotConnectedError(self._offline_hint())
            futu = self._sdk()
            try:
                ctx = futu.OpenQuoteContext(
                    host=self.cfg.host,
                    port=self.cfg.port,
                    is_async_connect=True,
                    security_firm=self._security_firm(),
                    ai_type=1,
                )
            except Exception as exc:  # pragma: no cover - SDK 构造异常
                raise FutuNotConnectedError(f"创建行情上下文失败: {exc}") from exc
            try:
                ctx.set_sync_query_connect_timeout(self.cfg.sync_query_timeout)
            except Exception:  # pragma: no cover - 老版本 SDK 无此方法
                pass
            self._quote_ctx = ctx
            return ctx

    def _wait_ready(self, ctx, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if str(getattr(ctx, "status", "")) in _READY_STATUSES:
                return True
            time.sleep(0.2)
        return str(getattr(ctx, "status", "")) in _READY_STATUSES

    # ------------------------------------------------------------------ 交易上下文
    @property
    def trade_ctx(self):
        """默认市场的交易上下文(等价于 `trade_ctx_for(None)`)。"""
        return self.trade_ctx_for(None)

    def trade_ctx_for(self, market: str | None):
        """取指定市场的交易上下文(A股/港股/美股各一个, 建好后缓存)。

        富途的 `OpenSecTradeContext` 带 `filter_trdmarket`, 一个上下文只覆盖一个市场;
        而 A 股和港股的资金账户是**不同**的, 所以必须按市场分别建连并分别选账户。

        `OpenSecTradeContext` 的构造函数在 OpenD 不可达时会 `while True` 无限重连,
        因此放到守护线程里构造, 主线程限时等待。

        **关键**: `join()` 必须在锁外进行 —— 后台线程要拿 `self._lock` 才能发布
        构造结果, 若主线程持锁等待, 就是死锁: 必然等到超时才失败, 而失败后锁一释放
        后台线程立刻完成, 于是"第二次调用就好了"。这个坑实测踩过。
        """
        key = self.cfg.market_key(market)
        ctx = self._trade_ctxs.get(key)
        if ctx is not None:
            return ctx
        with self._lock:
            ctx = self._trade_ctxs.get(key)
            if ctx is not None:
                return ctx
            if not self.probe():
                raise FutuNotConnectedError(self._offline_hint())
            thread = self._trade_threads.get(key)
            if thread is None or not thread.is_alive():
                self._trade_errors.pop(key, None)
                thread = threading.Thread(
                    target=self._build_trade_ctx, args=(key,),
                    name=f"futu-trade-connect-{key}", daemon=True,
                )
                self._trade_threads[key] = thread
                thread.start()
        # ---- 锁外等待(见 docstring: 持锁等待会死锁) ----
        thread.join(timeout=TRADE_CONNECT_TIMEOUT)
        with self._lock:
            ctx = self._trade_ctxs.get(key)
            err = self._trade_errors.get(key)
        if ctx is None:
            detail = f" ({err})" if err else ""
            # 端口通但交易上下文没起来, 说明不是"OpenD 没开", 别给误导性提示
            raise FutuNotConnectedError(
                f"OpenD 端口 {self.cfg.host}:{self.cfg.port} 可连通, 但 {key} 市场的交易上下文"
                f"在 {TRADE_CONNECT_TIMEOUT:.0f}s 内未就绪{detail}。"
                "通常是 OpenD 尚未完成**交易登录**(GUI 右侧「交易登录」)—— "
                "行情可用、交易不可用就是这个现象。稍等几秒重试通常即可。"
            )
        return ctx

    def _build_trade_ctx(self, key: str) -> None:
        try:
            futu = self._sdk()
            ctx = futu.OpenSecTradeContext(
                filter_trdmarket=self._trd_market(key),
                host=self.cfg.host,
                port=self.cfg.port,
                security_firm=self._security_firm(),
                ai_type=1,
            )
            try:
                ctx.set_sync_query_connect_timeout(self.cfg.sync_query_timeout)
            except Exception:  # pragma: no cover
                pass
            with self._lock:
                self._trade_ctxs[key] = ctx
        except BaseException as exc:  # pragma: no cover - 后台线程内的异常
            with self._lock:
                self._trade_errors[key] = exc

    # ------------------------------------------------------------------ 调用包装
    @staticmethod
    def _unwrap(api: str, result):
        """把 futu 的 `(ret, data)` 收敛成 data, 失败即抛 FutuApiError。"""
        ret, data = result
        if ret != RET_OK:
            raise FutuApiError(api, ret, str(data))
        return data

    def call_quote(self, api: str, *args, **kwargs):
        """调用行情接口并校验返回码。"""
        ctx = self.quote_ctx
        if str(getattr(ctx, "status", "")) not in _READY_STATUSES:
            # 异步连接尚未就绪 -> 限时等待, 避免把"正在连接"当成接口错误
            if not self._wait_ready(ctx, QUOTE_CONNECT_TIMEOUT):
                raise FutuNotConnectedError(
                    f"行情连接未就绪(状态 {getattr(ctx, 'status', '?')})。" + self._offline_hint()
                )
        fn = getattr(ctx, api, None)
        if fn is None:
            raise FutuApiError(api, -1, "当前 futu-api 版本不支持该接口")
        return self._unwrap(api, fn(*args, **kwargs))

    def call_trade(self, api: str, *args, market: str | None = None, **kwargs):
        """调用交易接口并校验返回码。

        Args:
            market: 用哪个市场的交易上下文(也决定默认账户); 省略则用配置里的默认市场。
                **注意**: 它只被本方法消费, 不会透传给 futu 接口。
        """
        ctx = self.trade_ctx_for(market)
        fn = getattr(ctx, api, None)
        if fn is None:
            raise FutuApiError(api, -1, "当前 futu-api 版本不支持该接口")
        return self._unwrap(api, fn(*args, **kwargs))

    # ------------------------------------------------------------------ 账户解析
    def resolve_acc_id(self, market: str | None = None) -> tuple[int, dict | None]:
        """确定本次要操作的资金账户。

        优先级:
        1. 配置里的 `acc_id`(显式覆盖);
        2. 配置里的 `acc_ids[<市场>]`(按市场固定, **推荐**);
        3. 兜底: 该市场账户列表里第 `acc_index` 个 `ACTIVE` 账户。

        兜底会把 REAL 账户也纳入候选, 所以生产用法应当配好 `acc_ids`。
        """
        key = self.cfg.market_key(market)
        if self.cfg.acc_id:
            return int(self.cfg.acc_id), None
        pinned = (self.cfg.acc_ids or {}).get(key)
        if pinned:
            return int(pinned), {"acc_id": int(pinned), "market": key, "source": "config.acc_ids"}
        accounts = self.call_trade("get_acc_list", market=key)
        rows = _records(accounts)
        if not rows:
            raise FutuApiError("get_acc_list", -1, f"{key} 市场没有可用账户: 请在 OpenD 完成交易登录")
        active = [r for r in rows if str(r.get("acc_status", "")).upper() == "ACTIVE"] or rows
        index = min(max(int(self.cfg.acc_index), 0), len(active) - 1)
        chosen = active[index]
        return int(chosen.get("acc_id", 0)), chosen

    # ------------------------------------------------------------------ 健康与生命周期
    def health(self, *, deep: bool = False) -> dict:
        """OpenD 连接/登录状态。

        Args:
            deep: True 时额外查询交易账户列表(需要交易登录)。
        """
        state: dict[str, Any] = {
            "host": self.cfg.host,
            "port": self.cfg.port,
            "tcp_reachable": self.probe(),
            "sdk_version": None,
            "connected": False,
        }
        try:
            futu = self._sdk()
            state["sdk_version"] = getattr(futu, "__version__", None) or _read_sdk_version(futu)
        except Exception as exc:
            state["sdk_error"] = str(exc)
            return state
        if not state["tcp_reachable"]:
            state["hint"] = self._offline_hint()
            return state
        try:
            data = self.call_quote("get_global_state")
            state["connected"] = True
            state["opend"] = _plain(data)
        except Exception as exc:
            state["error"] = str(exc)
            state["hint"] = self._offline_hint()
            return state
        if deep:
            try:
                acc_id, chosen = self.resolve_acc_id()
                state["acc_id"] = acc_id
                state["account"] = chosen
            except Exception as exc:
                state["trade_error"] = str(exc)
        return state

    def close(self) -> None:
        """关闭全部上下文(进程退出或需要强制重连时调用)。

        必须调用: futu 建连后会留下**非守护线程**, 不关闭则解释器无法退出。
        """
        with self._lock:
            if self._quote_ctx is not None:
                try:
                    self._quote_ctx.close()
                except Exception:  # pragma: no cover - 关闭失败不影响退出
                    pass
                self._quote_ctx = None
            for key, ctx in list(self._trade_ctxs.items()):
                try:
                    ctx.close()
                except Exception:  # pragma: no cover
                    pass
            self._trade_ctxs.clear()
            self._trade_threads.clear()
            self._trade_errors.clear()


# ---------------------------------------------------------------------- 工具函数
def _records(data) -> list[dict]:
    """把接口返回的 DataFrame / dict / list 统一成 list[dict]。"""
    if data is None:
        return []
    if isinstance(data, list):
        return [dict(x) if isinstance(x, dict) else {"value": x} for x in data]
    if isinstance(data, dict):
        return [data]
    if hasattr(data, "to_dict"):  # pandas.DataFrame
        try:
            return [_plain(r) for r in data.to_dict(orient="records")]
        except Exception:  # pragma: no cover
            return []
    return []


def _plain(value):
    """把 numpy/pandas 标量转成 JSON 可序列化的原生类型(NaN -> None)。"""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return None if value != value else value  # NaN -> None
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    if hasattr(value, "item"):
        try:
            return _plain(value.item())
        except Exception:  # pragma: no cover
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat(sep=" ") if value.__class__.__name__ in ("Timestamp", "datetime") else value.isoformat()
        except Exception:  # pragma: no cover
            pass
    if value != value:  # NaN 之外的不可比较对象
        return None
    return str(value)


def _read_sdk_version(futu) -> str | None:
    try:
        from pathlib import Path

        return (Path(futu.__file__).parent / "VERSION.txt").read_text(encoding="utf-8").strip()
    except Exception:  # pragma: no cover
        return None

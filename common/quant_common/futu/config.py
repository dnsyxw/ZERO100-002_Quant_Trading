"""富途 OpenAPI 接入配置。

优先级(高 -> 低):
1. 环境变量 (`FUTU_HOST` / `FUTU_PORT` / `FUTU_TRD_ENV` / ...)
2. `config/futu.json`
3. 本模块内置默认值

**安全约定 (重要)**
账户登录密码与交易解锁密码 **不进入本仓库、不进入代码、不进入环境变量**:
- OpenD 需要人工启动并在 GUI 上登录富途账号(牛牛号/手机号/邮箱);
- 交易解锁同样只能在 OpenD GUI 上手动完成 —— 富途官方安全规则明确禁止通过 SDK 的
  `unlock_trade` 接口解锁, 本项目不提供该能力(见 `docs/07_富途OpenAPI接入.md`)。
因此本文件只保存"连接参数 + 风控参数", 可以安全提交到 git。
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from .codes import normalize_market, to_trade_date_market
from .errors import FutuConfigError
from ..paths import REPO_ROOT, SHARED_CONFIG_DIR

__all__ = ["FutuConfig", "CONFIG_ENV_MAP", "DEFAULT_CONFIG_PATH"]

DEFAULT_CONFIG_PATH = SHARED_CONFIG_DIR / "futu.json"

#: 环境变量名 -> FutuConfig 字段名
CONFIG_ENV_MAP = {
    "FUTU_HOST": "host",
    "FUTU_PORT": "port",
    "FUTU_MARKET": "market",
    "FUTU_TRD_ENV": "trd_env",
    "FUTU_ACC_ID": "acc_id",
    "FUTU_ACC_INDEX": "acc_index",
    "FUTU_SECURITY_FIRM": "security_firm",
    "FUTU_SYNC_TIMEOUT": "sync_query_timeout",
    "FUTU_ENABLE_REAL_TRADE": "enable_real_trade",
    "FUTU_LOG_DIR": "log_dir",
    "FUTU_ORDER_LOG": "order_log",
}

_BOOL_TRUE = {"1", "true", "yes", "y", "on", "是", "真"}
_BOOL_FALSE = {"0", "false", "no", "n", "off", "否", "假"}


def _coerce(value, example):
    """按默认值的类型把字符串环境变量转成目标类型。"""
    if isinstance(example, bool):
        text = str(value).strip().lower()
        if text in _BOOL_TRUE:
            return True
        if text in _BOOL_FALSE:
            return False
        raise FutuConfigError(f"布尔值无法解析: {value!r}")
    if isinstance(example, int):
        return int(str(value).strip())
    if isinstance(example, float):
        return float(str(value).strip())
    return str(value)


@dataclass(frozen=True)
class FutuConfig:
    """富途接入层的全部可调参数。"""

    # ---- OpenD 网关 ----
    host: str = "127.0.0.1"
    port: int = 11111
    sync_query_timeout: float = 30.0

    # ---- 默认市场与代码 ----
    market: str = "CN"                  # CN(沪深) / HK / US; CN 会映射成 SH
    security_firm: str = "FUTUSECURITIES"

    # ---- 交易账户 ----
    trd_env: str = "SIMULATE"           # SIMULATE(模拟) / REAL(实盘)
    #: 按市场指定资金账户: `{"CN": 15188193, "HK": 15188192, "US": 15188191}`。
    #: **强烈建议填满并固定** —— 富途的 `acc_id=0`(自动取第一个)会把 DISABLED/REAL 账户
    #: 也纳入候选, 一旦有人打开实盘总闸就可能选错账户。实测这台机器上确实存在一个
    #: ACTIVE 的 REAL 账户。
    acc_ids: dict = field(default_factory=dict)
    acc_id: int = 0                     # 显式指定时覆盖 acc_ids; 0 = 用 acc_ids
    acc_index: int = 0                  # 两者都没配时的兜底: 取账户列表第 N 个
    enable_real_trade: bool = False     # 实盘总闸: False 时任何 REAL 下单都被拒绝

    # ---- 本地硬风控 (在 SDK 下单前生效) ----
    max_order_pct: float = 0.05         # 单笔买入金额 / 总资产 上限
    max_daily_buy_pct: float = 0.30     # 单日买入金额 / 总资产 上限

    # ---- 输出与留痕 ----
    log_dir: str = "runtime/futu_appdata"
    order_log: str = "runtime/futu_orders.jsonl"
    watchlist_group: str = "全部"        # 自选股分组名

    # ---- 展示与限额 ----
    default_kline_max: int = 500
    max_rows: int = 2000                # 单次工具调用返回的最大行数(防止撑爆上下文)

    def __post_init__(self) -> None:
        if not (0 < int(self.port) < 65536):
            raise FutuConfigError(f"port 非法: {self.port}")
        if str(self.trd_env).upper() not in ("SIMULATE", "REAL"):
            raise FutuConfigError(f"trd_env 只能是 SIMULATE 或 REAL, 收到 {self.trd_env!r}")
        if not (0 < float(self.max_order_pct) <= 1):
            raise FutuConfigError(f"max_order_pct 必须在 (0, 1] 内, 收到 {self.max_order_pct}")
        if not (0 < float(self.max_daily_buy_pct) <= 1):
            raise FutuConfigError(f"max_daily_buy_pct 必须在 (0, 1] 内, 收到 {self.max_daily_buy_pct}")
        if not isinstance(self.acc_ids, dict):
            raise FutuConfigError("acc_ids 必须是对象, 形如 {\"CN\": 123, \"HK\": 456}")
        for key, value in self.acc_ids.items():
            try:
                int(value)
            except (TypeError, ValueError):
                raise FutuConfigError(f"acc_ids[{key!r}] 必须是整数, 收到 {value!r}") from None

    # ---- 派生属性 ----
    @property
    def market_prefix(self) -> str:
        """配置市场名对应的富途市场前缀(CN -> SH), 用于拼证券代码。"""
        return normalize_market(self.market)

    @property
    def trade_date_market(self) -> str:
        """`request_trading_days` 需要的 `TradeDateMarket` 取值(A 股 -> CN)。

        富途对不同接口用了三套 market 枚举; 交易日历按国家/地区划分, A 股是 `CN`
        而不是代码前缀 `SH` —— 传错会报 "market is SH, which is not valid"。
        """
        return to_trade_date_market(self.market, default="CN")

    @property
    def is_simulate(self) -> bool:
        return str(self.trd_env).upper() == "SIMULATE"

    def market_key(self, market: str | None = None) -> str:
        """把任意市场写法收敛成账户映射用的键: `CN` / `HK` / `US` / ...。

        `SH`/`SZ`/`BJ`/`CN` 都归到 `CN`(A 股账户是同一个), 与 `acc_ids` 的键一致。
        """
        return to_trade_date_market(market, default=self.trade_date_market)

    def path(self, value: str) -> Path:
        """把配置里的相对路径解析成仓库内的绝对路径。"""
        p = Path(value)
        return p if p.is_absolute() else (REPO_ROOT / p)

    # ---- 装配 ----
    @classmethod
    def load(cls, path: Path | str | None = None, *, env: dict | None = None) -> "FutuConfig":
        """从 `config/futu.json` + 环境变量装配配置。"""
        cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
        data: dict = {}
        if cfg_path.exists():
            try:
                raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise FutuConfigError(f"{cfg_path} 不是合法 JSON: {exc}") from exc
            if not isinstance(raw, dict):
                raise FutuConfigError(f"{cfg_path} 顶层必须是对象")
            # 允许用 `_comment` / `_pending` 之类的下划线键写注释
            data = {k: v for k, v in raw.items() if not k.startswith("_")}

        known = {f for f in cls.__dataclass_fields__}
        unknown = sorted(set(data) - known)
        if unknown:
            raise FutuConfigError(f"{cfg_path} 存在未知字段: {', '.join(unknown)}")

        cfg = cls(**data)

        source = os.environ if env is None else env
        overrides = {}
        for env_name, field_name in CONFIG_ENV_MAP.items():
            if source.get(env_name) not in (None, ""):
                overrides[field_name] = _coerce(source[env_name], getattr(cfg, field_name))
        if overrides:
            cfg = replace(cfg, **overrides)
        return cfg

    # ---- 展示 ----
    def redacted(self) -> dict:
        """给模型/日志看的配置快照 —— 本配置本身不含密钥, 直接展开即可。"""
        data = asdict(self)
        data["market_prefix"] = self.market_prefix
        data["config_path"] = str(DEFAULT_CONFIG_PATH)
        data["secrets_required"] = [
            "OpenD 登录账号(牛牛号/手机号/邮箱) + 登录密码: 在 OpenD GUI 人工输入, 不写入本仓库",
            "交易解锁密码: 在 OpenD GUI 点击「解锁交易」人工输入(SDK 的 unlock_trade 被官方禁止使用)",
        ]
        return data

"""富途 (Futu) OpenAPI 接入层。

架构
----
```
OpenD (富途官方本地网关, 需人工启动并登录)
   ^  TCP 127.0.0.1:11111
   |
futu-api SDK  ──  quant_common.futu.gateway.FutuGateway   (连接/超时/健康检查)
                     ├── quant_common.futu.quote   行情能力 (快照/K线/摆盘/资金流...)
                     └── quant_common.futu.trade   交易能力 (账户/持仓/下单/撤单 + 硬风控)
   |
mcp/futu_server.py   把上述能力暴露成 MCP 工具 (mcp__futu__*), 供 AI Agent 调用
```

**不做什么(刻意)**
- 不做 OpenD 的账号登录 —— 必须人工在 GUI 完成;
- 不做交易解锁 —— 富途官方禁止通过 SDK 的 `unlock_trade` 解锁, 只能在 GUI 手动解锁;
- 不保存任何密码 / API key 到仓库;
- **不做任何融资 / 银证转账 / 贷款 / 换汇相关操作** —— 见 `safe_trade` 模块的红线说明。

三道下单闸门
------------
`quant_common.futu.safe_trade` 是**跨程序**的下单安全层。任何程序要下真单, 都应当
走 `place_cash_only_batch`, 它会强制: ① 只允许模拟盘; ② 只允许指定 acc_id
(禁止"自动挑一个账户"); ③ 买入总额不得超过可用现金(**绝不融资**)。
`AccountGuard.require_env` 被硬编码为 SIMULATE, 传 REAL 会直接抛异常。

用法::

    from quant_common.futu import FutuConfig, FutuGateway, quote, trade

    cfg = FutuConfig.load()
    gw = FutuGateway(cfg)
    print(quote.snapshot(gw, ["600000"]))
"""
from __future__ import annotations

from . import quote, safe_trade, trade
from .config import DEFAULT_CONFIG_PATH, FutuConfig
from .errors import (
    FutuApiError,
    FutuConfigError,
    FutuError,
    FutuNotConnectedError,
    FutuRiskRejected,
    FutuTradeDisabledError,
)
from .gateway import FutuGateway
from .safe_trade import AccountCheck, AccountGuard, place_cash_only_batch, verify_account

__all__ = [
    "FutuConfig",
    "FutuGateway",
    "DEFAULT_CONFIG_PATH",
    "quote",
    "trade",
    "safe_trade",
    "AccountGuard",
    "AccountCheck",
    "verify_account",
    "place_cash_only_batch",
    "FutuError",
    "FutuConfigError",
    "FutuNotConnectedError",
    "FutuApiError",
    "FutuTradeDisabledError",
    "FutuRiskRejected",
]

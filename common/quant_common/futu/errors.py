"""错误类型: 把富途 SDK 的 `(ret_code, data)` 返回风格收敛成异常语义。"""
from __future__ import annotations

__all__ = [
    "FutuError",
    "FutuConfigError",
    "FutuNotConnectedError",
    "FutuApiError",
    "FutuTradeDisabledError",
    "FutuRiskRejected",
]


class FutuError(Exception):
    """富途接入层所有错误的基类。"""


class FutuConfigError(FutuError):
    """配置缺失或非法(例如端口非法、试运行开关未打开却要求实盘)。"""


class FutuNotConnectedError(FutuError):
    """无法连接 OpenD, 或连接已断开。

    OpenD 是富途 API 的本地网关: 必须先人工启动并登录, SDK 才能连上。
    """


class FutuApiError(FutuError):
    """富途服务端返回了非 RET_OK 的结果。"""

    def __init__(self, api: str, code, message: str):
        self.api = api
        self.code = code
        self.message = message
        super().__init__(f"{api} 失败: [code={code}] {message}")


class FutuTradeDisabledError(FutuError):
    """配置未允许该交易动作 —— 属于主动安全拦截, 不是故障。"""


class FutuRiskRejected(FutuError):
    """下单被本地硬风控拒绝。"""

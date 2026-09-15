"""证券代码格式互转: 本项目内部格式 <-> 富途 (Futu) 格式。

本项目内部沿用 baostock 风格: `600000` / `000001` / `000852.SH`(指数后缀)。
富途使用 `市场.代码`: `SH.600000` / `SZ.000001` / `SH.000852` / `HK.00700` / `US.AAPL`。

转换规则(仅用于 A 股 6 位数字代码):
- `6` / `9` / `5` 开头 -> 上交所 (`SH`)
- `0` / `3` / `2` / `1` 开头 -> 深交所 (`SZ`)
- `4` / `8` 开头 -> 北交所 (`BJ`)
"""
from __future__ import annotations

import re

__all__ = [
    "to_futu",
    "from_futu",
    "normalize_market",
    "split_futu",
    "market_of",
    "to_trade_date_market",
]

# 富途代码: 市场前缀 + 点 + 代码, 例如 SH.600000 / HK.00700 / US.AAPL
_FUTU_RE = re.compile(r"^(?P<market>[A-Za-z]{2})\.(?P<code>[A-Za-z0-9._\-]+)$")
# 本项目格式: 6 位数字, 可选 .SH/.SZ/.BJ 后缀
_LOCAL_RE = re.compile(r"^(?P<code>\d{6})(?:\.(?P<suffix>SH|SZ|BJ))?$", re.IGNORECASE)

_SH_PREFIXES = ("6", "9", "5")
_SZ_PREFIXES = ("0", "3", "2", "1")
_BJ_PREFIXES = ("4", "8")

# 富途 market 取值 -> 本项目/配置文件使用的市场名
_MARKET_ALIAS = {
    "SH": "SH", "SZ": "SZ", "BJ": "BJ",
    "HK": "HK", "US": "US", "SG": "SG", "JP": "JP", "MY": "MY", "CA": "CA",
}
# 配置文件里可写的市场名 -> 富途市场前缀
_CONFIG_MARKET = {
    "CN": "SH", "A": "SH", "ASHARE": "SH",
    "SH": "SH", "SZ": "SZ", "BJ": "BJ",
    "HK": "HK", "US": "US", "SG": "SG", "JP": "JP", "MY": "MY",
}


def _infer_a_share_market(code: str) -> str:
    head = code[0]
    if head in _SH_PREFIXES:
        return "SH"
    if head in _SZ_PREFIXES:
        return "SZ"
    if head in _BJ_PREFIXES:
        return "BJ"
    raise ValueError(f"无法判断 {code} 属于哪个 A 股市场(仅支持 6/9/5/0/3/2/1/4/8 开头)")


def to_futu(code: str, market: str | None = None) -> str:
    """把本项目内部代码转成富途代码。

    Args:
        code: `600000` / `000001.SZ` / `SH.600000` / `HK.00700` / `AAPL` 等。
        market: 无市场信息时使用的默认市场 (如 `US`/`HK`)。A 股可由代码前缀推断。

    Returns:
        富途格式代码, 例如 `SH.600000`。
    """
    raw = str(code).strip()
    if not raw:
        raise ValueError("代码不能为空")

    m = _FUTU_RE.match(raw)
    if m:  # 已经是富途格式
        mkt = m.group("market").upper()
        body = m.group("code")
        if mkt == "US":
            body = body.upper()
        return f"{mkt}.{body}"

    m = _LOCAL_RE.match(raw)
    if m:
        body = m.group("code")
        suffix = (m.group("suffix") or "").upper()
        mkt = suffix or _infer_a_share_market(body)
        return f"{mkt}.{body}"

    if raw.isdigit():  # 港股等纯数字代码
        mkt = normalize_market(market, default="HK")
        return f"{mkt}.{raw.zfill(5) if mkt == 'HK' else raw}"

    if market:  # 美股等字母代码
        mkt = normalize_market(market)
        return f"{mkt}.{raw.upper()}"
    raise ValueError(f"无法识别的代码: {code!r}")


def split_futu(code: str) -> tuple[str, str]:
    """拆分富途代码为 `(市场, 代码)`, 例如 `SH.600000` -> `("SH", "600000")`。"""
    m = _FUTU_RE.match(str(code).strip())
    if not m:
        raise ValueError(f"不是合法的富途代码: {code!r}")
    return m.group("market").upper(), m.group("code")


def from_futu(code: str) -> str:
    """把富途代码转成本项目内部代码。

    A 股去前缀返回 6 位数字; 港股去掉前导零; 美股保持原样。
    """
    market, body = split_futu(code)
    if market in ("SH", "SZ", "BJ"):
        return body
    if market == "HK":
        return body.lstrip("0") or "0"
    return body


def normalize_market(market: str | None, *, default: str = "SH") -> str:
    """把配置里的市场名规范化成富途市场前缀 (大写)。"""
    if not market:
        return default
    key = str(market).strip().upper()
    return _CONFIG_MARKET.get(key, _MARKET_ALIAS.get(key, key))


def market_of(code: str, *, default: str = "SH") -> str:
    """从代码本身推断所属市场(前缀优先), 推断不出来才用 default。

    用于"不显式传 market 也要选对交易账户"的场景:
    `HK.01810` -> `HK`, `600000` -> `SH`, `AAPL` + default=US -> `US`。
    """
    raw = str(code).strip()
    m = _FUTU_RE.match(raw)
    if m:
        return m.group("market").upper()
    m = _LOCAL_RE.match(raw)
    if m:
        return (m.group("suffix") or _infer_a_share_market(m.group("code"))).upper()
    return normalize_market(default)


#: 富途有**三套不同的 market 枚举**, 用错了接口会直接报 "market is SH, which is not valid":
#:   - `Market`            (SH/SZ/HK/US/SG/JP/MY/CA/AU/FX/CC/EC/HK_FUTURE/NONE)
#:                         -> get_plate_list / get_stock_basicinfo / get_stock_filter
#:   - `TradeDateMarket`   (CN/HK/US/SG/JP/MY/NT/ST/NONE)
#:                         -> request_trading_days  ← A 股要传 CN, 不是 SH
#:   - `TrdMarket`         (CN/HK/US/SG/JP/MY/ AU/CA/CRYPTO/FUTURES/...)
#:                         -> 交易上下文 filter_trdmarket
_TRADE_DATE_MARKET = {
    "CN": "CN", "SH": "CN", "SZ": "CN", "BJ": "CN", "A": "CN", "ASHARE": "CN",
    "HK": "HK", "US": "US", "SG": "SG", "JP": "JP", "MY": "MY", "CA": "CA",
}


def to_trade_date_market(market: str | None, *, default: str = "CN") -> str:
    """把市场名转成 `request_trading_days` 需要的 `TradeDateMarket` 取值。

    与代码前缀(`SH`/`SZ`)不同: 交易日历按**国家/地区**划分, A 股统一是 `CN`。
    """
    if not market:
        return default
    key = str(market).strip().upper()
    key = _CONFIG_MARKET.get(key, key)
    return _TRADE_DATE_MARKET.get(key, key)

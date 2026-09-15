"""港股代码规范与股票池分类规则。

代码格式
--------
本项目统一用 **`00700.HK`** 形式(5 位数字 + `.HK`), 与 A 股的 `600519.SH` 风格一致。
- 新浪/腾讯/富途都用 `00700` / `hk00700` / `HK.00700`, 转换函数在本模块集中处理。
- **必须保留前导零**: 港股代码恒为 5 位, `700` 与 `00700` 是两个不同的 key,
  丢零会造成缓存与因子矩阵错位。

板块与"非个股"判定(港股特有的坑)
--------------------------------
1. **GEM(创业板)**: 代码 `08xxx`。流动性差、仙股比例极高, 默认排除。
2. **人民币柜台**: 名称后缀 `-R`(如 `港交所-R`), 与港币柜台是**同一家公司的重复标的**,
   保留会造成同一只股票在组合里出现两次。默认排除。
3. **合订证券/对冲工具**: 名称后缀 `-SS`(如 `港灯-SS`)。默认排除。
4. **同股不同权 `-W` / 生物科技 `-B`**: **保留** —— 它们是真实权益(阿里-W、小米-W),
   排除会丢掉港股最重要的新经济板块; 但名称中的后缀必须**保留**, 否则与同名旧股混淆。
5. **基金/ETF/权证/结构性产品**: 新浪现货快照不区分证券类型, 只能靠代码段 + 名称关键词
   近似识别(见 `is_probable_equity`)。真正的清洗靠 `min_price` / `min_amt20` 流动性下限。

每手股数(lot size)
------------------
**港股每手股数因股票而异**(腾讯 100、汇丰 400、长和 500、部分小盘 10000/20000)。
整手取整是港股回测不可省略的一步: 一手 20000 股 × 0.5 港元 = 1 万港元,
而一手 500 股 × 700 港元 = 35 万港元 —— 后者会让"等权 30 只 / 100 万组合"根本无法建仓。
本模块提供 `normalize_lot_size` 做兜底, 真实值由 `store.load_registry()` 提供。
"""
from __future__ import annotations

import re

__all__ = [
    "MARKET_SUFFIX",
    "to_std_code",
    "to_digits",
    "to_sina_symbol",
    "to_tencent_symbol",
    "to_futu_code",
    "board_of",
    "is_gem",
    "is_rmb_counter",
    "is_ss_counter",
    "is_probable_equity",
    "name_suffix",
    "normalize_lot_size",
    "DEFAULT_LOT_SIZE",
]

MARKET_SUFFIX = ".HK"

DEFAULT_LOT_SIZE = 1000  # 兜底每手股数(港股最常见值之一; 缺失时宁可取得偏大)

#: 港股常见每手股数(用于把异常值夹到合法集合)
_COMMON_LOTS = (20, 25, 50, 100, 200, 250, 300, 400, 500, 600, 800, 1000,
                1200, 1500, 2000, 2500, 3000, 4000, 5000, 6000, 8000, 10000,
                20000, 30000, 50000, 100000)

_DIGITS_RE = re.compile(r"(\d{1,5})")


def to_digits(code: str) -> str:
    """任意写法 -> 5 位数字字符串(保留前导零)。"""
    s = str(code).strip().upper()
    for prefix in ("HK.", "HK", "SH.", "SZ."):
        if s.startswith(prefix):
            s = s[len(prefix):]
    s = s.replace(".HK", "").replace(".hk", "")
    m = _DIGITS_RE.search(s)
    if not m:
        raise ValueError(f"无法解析港股代码: {code!r}")
    return m.group(1).zfill(5)


def to_std_code(code: str) -> str:
    """任意写法 -> 规范 `00700.HK`。"""
    return f"{to_digits(code)}{MARKET_SUFFIX}"


def to_sina_symbol(code: str) -> str:
    """规范代码 -> 新浪 symbol(5 位数字, 如 `00700`)。"""
    return to_digits(code)


def to_tencent_symbol(code: str) -> str:
    """规范代码 -> 腾讯 symbol(如 `hk00700`)。"""
    return f"hk{to_digits(code)}"


def to_futu_code(code: str) -> str:
    """规范代码 -> 富途代码(如 `HK.00700`)。"""
    return f"HK.{to_digits(code)}"


def board_of(code: str) -> str:
    """返回板块标签: `GEM`(08xxx) / `MAIN`(其余)。"""
    d = to_digits(code)
    return "GEM" if d.startswith("08") else "MAIN"


def is_gem(code: str) -> bool:
    """是否 GEM(创业板)标的 —— 代码 `08xxx`。"""
    return board_of(code) == "GEM"


def name_suffix(name: str) -> frozenset[str]:
    """从名称里抽出港股的柜台/权益后缀标记。

    返回集合, 可能同时含多个(如 `XXX-B-R` → `{'-B', '-R'}`)。
    """
    n = str(name).upper().strip()
    found = set()
    for suf in ("-R", "-SS", "-B", "-W", "-S", "-P"):
        if n.endswith(suf):
            found.add(suf)
            n = n[: -len(suf)]
    return frozenset(found)


def is_rmb_counter(name: str) -> bool:
    """是否人民币柜台(`-R`) —— 与港币柜台重复, 应排除。"""
    return "-R" in name_suffix(name)


def is_ss_counter(name: str) -> bool:
    """是否合订证券(`-SS`) —— 非普通权益, 应排除。"""
    return "-SS" in name_suffix(name)


#: 名称/代码里出现这些关键词的极可能不是普通个股(基金/权证/债券/优先股)
_NON_EQUITY_KEYWORDS = (
    "ETF", "基金", "债券", "债", "票据", "优先股", "权证", "牛熊", "认购", "认沽",
    "杠杆", "反向", "REIT", "TRUST", "FUND", "BOND", "NOTE", "WARRANT", "CBBC",
    "INVERSE", "LEVERAGE", "ETF-", "黄金", "GOLD", "白银", "SILVER", "期货",
)


def is_probable_equity(code: str, name: str, *, allow_gem: bool = False) -> bool:
    """启发式判断"这是不是一只可以纳入多因子选股的普通股"。

    只做**明显非个股**的排除(基金/权证/债券/重复柜台/GEM); 真正的质量过滤交给
    股票池的流动性下限(见 `strategy.universe`) —— 因为港股仙股占数量一半以上,
    用关键词穷举清洗既不可靠也不必要。

    Args:
        code: 港股代码。
        name: 中文名称(可为空)。
        allow_gem: 是否允许 GEM 标的(默认 False)。
    """
    if not allow_gem and is_gem(code):
        return False
    n = str(name or "").upper()
    if is_rmb_counter(n) or is_ss_counter(n):
        return False
    return not any(k in n for k in _NON_EQUITY_KEYWORDS)


def normalize_lot_size(value, *, default: int = DEFAULT_LOT_SIZE) -> int:
    """把每手股数规范成"合法且可用"的正整数。

    港股每手股数的真实取值很杂(从 20 到 100000), 且数据源常给出 `0` / `NaN` / `N/A`
    (富途对 934 只标的返回 0)。整手取整不能用一个错的 lot:
    - 缺失/非正 -> 取 `default`(偏大, 即**更保守**, 不会高估可建仓性);
    - 合法值 -> 原样返回;
    - 异常小的值(<20) -> 抬到 20, 避免"1 股 1 手"这种不可能值把回测变成连续权重。

    注意: 这是**兜底**。回测与下单应优先使用 `store.load_registry()` 里的真实每手股数。
    """
    try:
        v = int(value)
    except (TypeError, ValueError):
        return int(default)
    if v <= 0:
        return int(default)
    if v < 20:
        return 20
    return v

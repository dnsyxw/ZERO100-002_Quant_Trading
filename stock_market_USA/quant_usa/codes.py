"""美股代码规范与股票池分类规则。

代码格式
--------
本项目统一用 **`AAPL.US`** 形式(大写字母根 + `.US` 后缀), 与 A 股的 `600519.SH`、
港股的 `00700.HK` 风格一致。

三个市场里**美股的代码是最麻烦的**, 因为各数据源对"股份类别"的写法互不兼容:

| 写法 | 例(伯克希尔 B 股) | 用在哪 |
|---|---|---|
| `BRK.B.US` | 本项目规范形式 | 内部 key / 缓存文件名 / 因子矩阵列名 |
| `BRK.B` | 新浪美股清单与日K接口的 `symbol` | `source.SinaUSSource` |
| `BRK-B` | Yahoo Finance | (本项目仅作对照, 不作数据源) |
| `gb_brk$b` | 新浪 `hq.sinajs.cn` 实时行情 | 现货快照兜底 |

`to_std_code` / `to_sina_symbol` / `to_sina_hq_symbol` 三个函数把这三种写法集中转换,
**不要在别处手写 replace**。

US 市场特有的股票池陷阱
-----------------------
1. **OTC / 粉单(PINK)**: 新浪清单里带 `market` 字段可判定。粉单没有统一的报价与
   结算保障、价差可达 10%+, 且充斥着无财报的空壳 —— 默认排除。
2. **优先股 / 存托凭证 / 权证 / 单位 / 权利**: 代码后缀 `-PR*`(`BAC.PRA`)、
   `-WS`/`.WS`(`AACT.WS`)、`-U`(`XXXU`)、`-RT`。它们**不是普通股**,
   波动率与收益分布完全不同, 混进来会污染因子截面。默认排除。
3. **ADR(美国存托凭证)**: 保留 —— ADR 是美股市场里真实可交易、流动性充足的权益
   (阿里/BABA、台积电/TSM、阿斯利康/AZN 都是 ADR), 剔除会丢掉一大块可投域。
4. **SPAC / 空白支票公司**: 代码常以 `U`/`WS` 结尾(`XXXU`)。这类标的在完成并购前
   的"股价"几乎是面值 10 美元的现金等价物, 毫无量价信息 —— 排除。
5. **双类别上市**(`GOOGL`/`GOOG`、`FOX`/`FOXA`): 两家都是真实权益,
   **保留**, 但由此带来的"同一公司占两个仓位"由 `composite_score` 的权重等分自然分摊。

与 A 股/港股的对照(为什么这套分类不能复用)
------------------------------------------
- A 股靠代码前缀(`60`/`00`/`30`/`68`)判板块, 港股靠 `08xxx` 判 GEM —— 美股**没有
  数字代码**, 代码是纯文本, 只能靠**后缀模式 + 名单里的字段**判定。
- 美股**每手 1 股**(无整手概念), 因此本模块不提供 `normalize_lot_size`;
  引擎里的 `lot_size` 恒为 1(见 `quant_usa.engine` 顶部说明)。
"""
from __future__ import annotations

import re

__all__ = [
    "MARKET_SUFFIX",
    "to_std_code",
    "to_root",
    "to_sina_symbol",
    "to_sina_hq_symbol",
    "to_yahoo_symbol",
    "class_suffix",
    "is_preferred",
    "is_warrant",
    "is_unit",
    "is_right",
    "is_probable_equity",
    "is_probable_common_stock",
    "EXCHANGES",
]

MARKET_SUFFIX = ".US"

#: 新浪美股清单 `market` 字段的合法取值(本项目只接受前三个)
EXCHANGES: tuple[str, ...] = ("NASDAQ", "NYSE", "AMEX", "NYSEARCA", "BATS", "PINK", "OTC")

#: 常见的**非普通股**后缀(小写比较)。注意 `-`/`.`/`$` 三种分隔符都要处理:
#: 新浪用 `.`(`BAC.PRA`), Yahoo 用 `-`(`BAC-PRA`)。
_PREFERRED_PAT = re.compile(r"[.\-]PR[A-Z]?$", re.IGNORECASE)
_WARRANT_PAT = re.compile(r"[.\-](WS|WT|W)$", re.IGNORECASE)
_UNIT_PAT = re.compile(r"[.\-](U|UN)$", re.IGNORECASE)
_RIGHT_PAT = re.compile(r"[.\-](RT|R)$", re.IGNORECASE)
_WHEN_ISSUED_PAT = re.compile(r"[.\-]WI$", re.IGNORECASE)

#: 代码根里出现这些字符的极可能是衍生品/特殊结构, 直接排除
_BAD_ROOT_CHARS = set("^/\\ =,")

_DIGITS_ONLY = re.compile(r"^\d+$")

#: 公司名里出现这些关键词的极可能不是普通股(基金/信托/债券/权证/特殊目的收购公司)
_NON_EQUITY_KEYWORDS = (
    "ETF", "ETN", "FUND", "TRUST", "BOND", "NOTE", "WARRANT", "RIGHT",
    "PREFERRED", "DEPOSITARY SHARE", "UNIT", "CLOSED END", "MUTUAL",
    "ACQUISITION CORP", "BLANK CHECK", "SPAC", "ROYALTY TRUST",
    "INCOME FUND", "INDEX FUND", "MONEY MARKET",
)


def to_root(symbol: str) -> str:
    """任意写法 -> 大写代码根(去掉市场后缀, 分隔符统一保留)。

    `brk.b.US` / `gb_brk$b` / `BRK-B` -> `BRK.B`
    """
    s = str(symbol).strip().upper()
    if s.startswith("GB_"):
        s = s[3:]
    for suf in (".US", ".us"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    s = s.replace("$", ".").replace("-", ".")
    return s.strip()


def to_std_code(symbol: str) -> str:
    """任意写法 -> 规范 `AAPL.US` / `BRK.B.US`。

    Raises:
        ValueError: 空代码或含非法字符。
    """
    root = to_root(symbol)
    if not root:
        raise ValueError(f"无法解析美股代码: {symbol!r}")
    if _BAD_ROOT_CHARS & set(root):
        raise ValueError(f"美股代码含非法字符: {symbol!r}")
    return f"{root}{MARKET_SUFFIX}"


def to_sina_symbol(code: str) -> str:
    """规范代码 -> 新浪清单/日K接口的 `symbol`(保留点号, 如 `BRK.B`)。"""
    return to_root(code)


def to_sina_hq_symbol(code: str) -> str:
    """规范代码 -> 新浪实时行情 symbol(`gb_` 前缀, 点号换成 `$`)。

    新浪的 `hq.sinajs.cn` 用 `gb_brk$b` 表示伯克希尔 B 股 —— 点号必须换成 `$`,
    否则会静默返回空行。
    """
    return "gb_" + to_root(code).lower().replace(".", "$")


def to_yahoo_symbol(code: str) -> str:
    """规范代码 -> Yahoo Finance symbol(点号换成 `-`)。仅供对照/校验使用。"""
    return to_root(code).replace(".", "-")


def class_suffix(code: str) -> str:
    """返回代码的类别后缀(无后缀返回空串)。

    `BRK.B` -> `.B`;  `BAC.PRA` -> `.PRA`;  `AAPL` -> ``。
    """
    root = to_root(code)
    return f".{root.split('.', 1)[1]}" if "." in root else ""


def is_preferred(code: str) -> bool:
    """是否优先股(`BAC.PRA` / `XYZ.PRB` / Yahoo 的 `XYZ-PRB`)。"""
    return bool(_PREFERRED_PAT.search(to_root(code)))


def is_warrant(code: str) -> bool:
    """是否权证(`AACT.WS` / `XYZ.WT` / `XYZ.W`)。"""
    return bool(_WARRANT_PAT.search(to_root(code)))


def is_unit(code: str) -> bool:
    """是否单位(SPAC 的 unit, `XXX.U` / `XXX.UN`)或**SPAC 普通股** `XXXU`。"""
    root = to_root(code)
    if _UNIT_PAT.search(root):
        return True
    # 新浪清单里 SPAC 普通股直接是 `XXXXU`(5 个字母以上且以 U 结尾, 且不是常见词)
    return len(root) >= 4 and root.endswith("U") and root.isalpha()


def is_right(code: str) -> bool:
    """是否配股权(`XYZ.RT` / `XYZ.R`)。"""
    return bool(_RIGHT_PAT.search(to_root(code)))


def is_when_issued(code: str) -> bool:
    """是否 when-issued 临时代码(`XYZ.WI`)。"""
    return bool(_WHEN_ISSUED_PAT.search(to_root(code)))


def is_probable_common_stock(code: str) -> bool:
    """代码层面判断"这是不是普通股"(不看名称)。

    这是**第一道**过滤(便宜、无歧义); 第二道是名称关键词(见 `is_probable_equity`),
    第三道是股票池的流动性/价格下限(`quant_usa.universe`)。
    """
    root = to_root(code)
    if not root or _BAD_ROOT_CHARS & set(root):
        return False
    if _DIGITS_ONLY.match(root):        # 纯数字(部分粉单/外企代码) -> 无法可靠判定
        return False
    if len(root) > 6:                   # 美股普通股代码极少超过 5 个字符(含类别后缀 6)
        return False
    return not (is_preferred(root) or is_warrant(root) or is_unit(root)
                or is_right(root) or is_when_issued(root))


def is_probable_equity(code: str, name: str = "", market: str = "") -> bool:
    """代码 + 名称 + 交易所 三者合并判断"这是不是一只可用于多因子选股的普通股"。

    Args:
        code: 美股代码(任意写法)。
        name: 公司名(来自新浪清单, 可为空)。
        market: 交易所标签(来自新浪清单, 如 `NASDAQ` / `PINK`; 可为空)。

    Returns:
        True 表示"可以进入候选池"; 真正的质量过滤交给 `quant_usa.universe` 的
        ADTV/价格下限 —— 这里只排除**结构性**的非普通股。
    """
    if not is_probable_common_stock(code):
        return False
    if market:
        m = str(market).strip().upper()
        # 粉单/OTC: 无统一报价与结算保障, 价差极大 -> 默认排除
        if m in ("PINK", "OTC", "OTCBB", "GREY", "OTCQX", "OTCQB"):
            return False
    n = str(name or "").upper()
    return not any(k in n for k in _NON_EQUITY_KEYWORDS)

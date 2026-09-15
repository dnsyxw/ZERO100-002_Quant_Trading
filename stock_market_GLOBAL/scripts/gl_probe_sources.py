"""数据源探针 —— 验证本机当前能拿到哪些多资产 ETF 历史, 以及拿到多长。

为什么要留一个探针脚本: 本程序的数据源取舍(**选 Yahoo 而不是新浪**)与仓库
美股侧的结论**正好相反**, 理由必须可复核而不是"我记得"。这个脚本把当时的实测
重跑一遍: Yahoo 的覆盖区间、总收益(adjclose)是否存在、以及新浪在同一批标的上的缺口。

用法::

    python stock_market_GLOBAL\\scripts\\gl_probe_sources.py
    python stock_market_GLOBAL\\scripts\\gl_probe_sources.py --sina   # 同时对比新浪
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_PROJ = ROOT.parent
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from quant_common.futu._bootstrap import ensure_utf8_stdio  # noqa: E402
from quant_global import universe  # noqa: E402


def probe_yahoo(symbols: list[str]) -> dict[str, tuple]:
    """Yahoo chart API: 能否拿到 adjclose(总收益)以及多长的历史。"""
    from curl_cffi import requests as creq

    url = ("https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
           "?period1=0&period2=9999999999&interval=1d&events=div%2Csplit")

    def one(sym: str):
        try:
            r = creq.get(url.format(sym=sym), impersonate="chrome", timeout=30)
            if r.status_code != 200:
                return sym, f"HTTP {r.status_code}", "", "", 0
            res = ((r.json().get("chart") or {}).get("result") or [])
            if not res:
                return sym, "空响应", "", "", 0
            ts = res[0].get("timestamp") or []
            adj = ((res[0].get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose")
            if not ts:
                return sym, "无时间戳", "", "", 0
            import time as _t
            d0 = _t.strftime("%Y-%m-%d", _t.gmtime(ts[0]))
            d1 = _t.strftime("%Y-%m-%d", _t.gmtime(ts[-1]))
            return sym, ("有 adjclose" if adj else "**无 adjclose**"), d0, d1, len(ts)
        except Exception as e:  # noqa: BLE001
            return sym, f"{type(e).__name__}: {e}"[:40], "", "", 0

    out = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        for sym, st, d0, d1, n in ex.map(one, symbols):
            out[sym] = (st, d0, d1, n)
    return out


def probe_sina(symbols: list[str]) -> dict[str, tuple]:
    """新浪美股日K(仓库美股侧的主源)在同一批 ETF 上的覆盖 —— 用它证明缺口。

    **为什么这里自己写十几行而不用 `quant_usa.source`**:
    本仓库有一条硬约定 —— 四个市场/组合程序互不 import(`tools/tests/test_launcher.py`
    的 `Test程序隔离` 守着它)。探针如果 import `quant_usa`, 改美股代码就会影响组合程序。
    代价是重复一个 JSONP 端点的解析; 收益是"为什么选 Yahoo"这件事可以被独立复核。
    """
    import json
    import re
    import urllib.request

    url = ("https://stock.finance.sina.com.cn/usstock/api/jsonp.php/"
           "IO.XSRV2.CallbackList/US_MinKService.getDailyK?symbol={sym}&___qn=3")

    def one(sym: str):
        try:
            req = urllib.request.Request(url.format(sym=sym),
                                         headers={"User-Agent": "Mozilla/5.0"})
            text = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
            m = re.search(r"\[.*\]", text, re.S)
            recs = json.loads(m.group(0)) if m else []
            if not recs:
                return sym, "空", "", "", 0
            dates = sorted(str(r.get("d", "")) for r in recs if r.get("d"))
            return sym, "OK", dates[0], dates[-1], len(dates)
        except Exception as e:  # noqa: BLE001
            return sym, f"{type(e).__name__}"[:30], "", "", 0

    out = {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        for sym, st, d0, d1, n in ex.map(one, symbols):
            out[sym] = (st, d0, d1, n)
    return out


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdio()
    ap = argparse.ArgumentParser(description="多资产 ETF 数据源探针")
    ap.add_argument("--basket", default="core")
    ap.add_argument("--sina", action="store_true", help="同时对比新浪美股源")
    args = ap.parse_args(argv)

    syms = universe.symbols(args.basket) + [universe.CASH_PROXY]
    print(f"探针标的: {len(syms)} 只\n")
    yh = probe_yahoo(syms)
    sn = probe_sina(syms) if args.sina else {}

    head = f"{'标的':<6} {'Yahoo':<14} {'起始':<12} {'结束':<12} {'行数':>6}"
    if args.sina:
        head += f"   | {'新浪':<10} {'起始':<12} {'行数':>6}"
    print(head)
    print("-" * len(head))
    for s in syms:
        st, d0, d1, n = yh[s]
        line = f"{s:<6} {st:<14} {d0:<12} {d1:<12} {n:>6}"
        if args.sina:
            s2, e0, _e1, n2 = sn[s]
            line += f"   | {s2:<10} {e0:<12} {n2:>6}"
        print(line)

    print("\n结论(见 docs/12 §6.1):")
    print("  · Yahoo 提供 adjclose(含分红再投资的总收益), 覆盖到各 ETF 成立日 -> 回测能覆盖 2008;")
    print("  · 新浪不含分红, 且 TLT/IEF/SHY/EMB 只有 2016 年以后 -> 会丢掉 2008;")
    print("  · 本用例下标的都是当前仍上市的 ETF, Yahoo'退市股 404' 的缺陷不适用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

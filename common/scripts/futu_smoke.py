"""富途接入冒烟测试 —— OpenD 登录后跑这一条，证明整条链路真的通了。

用法::

    python scripts/futu_smoke.py                 # 用配置里的默认标的(中证1000 / 平安银行)
    python scripts/futu_smoke.py --code 600519   # 指定标的
    python scripts/futu_smoke.py --trade         # 额外验证账户/资金/持仓(需交易登录)
    python scripts/futu_smoke.py --json          # 输出 JSON, 便于机器读取

退出码 0 = 全通; 1 = 有环节失败(每项都会打印原因与处理建议)。
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]        # common/
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from quant_common.futu import FutuConfig, FutuGateway, quote, trade  # noqa: E402
from quant_common.futu._bootstrap import ensure_utf8_stdio  # noqa: E402

OK = "[ OK ]"
FAIL = "[FAIL]"
SKIP = "[SKIP]"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None, help="配置文件路径(默认 config/futu.json)")
    ap.add_argument("--code", default="600000", help="测试用标的(默认 600000)")
    ap.add_argument("--index", default="000852.SH", help="测试用指数代码(默认中证1000)")
    ap.add_argument("--kline-max", type=int, default=20, help="K 线测试根数(默认 20, 省额度)")
    ap.add_argument("--trade", action="store_true", help="额外验证交易账户/资金/持仓")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = ap.parse_args()

    ensure_utf8_stdio()
    cfg = FutuConfig.load(args.config)
    gw = FutuGateway(cfg)
    results: list[dict] = []

    def step(name: str, fn, *, quiet: bool = False):
        try:
            payload = fn()
            results.append({"step": name, "ok": True, "detail": payload})
            if not args.json and not quiet:
                print(f"{OK} {name}")
                if isinstance(payload, dict):
                    brief = {k: v for k, v in list(payload.items())[:4]}
                    print(f"       {json.dumps(brief, ensure_ascii=False, default=str)[:220]}")
            return payload
        except Exception as exc:
            results.append({"step": name, "ok": False,
                            "error": f"{type(exc).__name__}: {exc}",
                            "traceback": traceback.format_exc().splitlines()[-3:]})
            if not args.json and not quiet:
                print(f"{FAIL} {name} — {type(exc).__name__}: {exc}")
            return None

    if not args.json:
        print("=" * 72)
        print(f"富途接入冒烟测试  OpenD={cfg.host}:{cfg.port}  市场={cfg.market}  环境={cfg.trd_env}")
        print("=" * 72)

    # 1) 连接与登录状态 —— health() 不抛异常也能反映"连不上", 所以先静默取结果再判定
    health = step("OpenD 连接与登录状态 (get_global_state)",
                  lambda: gw.health(deep=args.trade), quiet=True)
    if health is not None and health.get("connected"):
        if not args.json:
            print(f"{OK} OpenD 连接与登录状态 (get_global_state)")
            opend = health.get("opend", {})
            print(f"       服务器 {opend.get('server_ver')} | 行情登录 {opend.get('qot_logined')} "
                  f"| 交易登录 {opend.get('trd_logined')} | SDK {health.get('sdk_version')}")
    else:
        reason = (health or {}).get("hint") or (health or {}).get("error") or "未连接"
        if results:
            results[-1] = {"step": results[-1]["step"], "ok": False, "error": reason}
        if not args.json:
            print(f"{FAIL} 未连上 OpenD — {reason[:220]}")
    if not health or not health.get("connected"):
        if not args.json:
            print("\n链路在第一关就断了, 后续步骤无意义。")
            print("处理: 1) 启动 Futu OpenD 桌面端并登录; 2) 确认端口与 config/futu.json 一致;")
            print("      3) 重跑 python scripts/check_futu_env.py")
        else:
            print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
        return 1

    # 2) 快照(不占订阅额度)
    step(f"快照 {args.code} (get_market_snapshot)", lambda: quote.snapshot(gw, [args.code]))

    # 3) 历史 K 线(占 7 天额度, 只取少量)
    step(f"历史K线 {args.index} (request_history_kline)",
         lambda: quote.history_kline(gw, args.index, ktype="K_DAY", autype="qfq", max_count=args.kline_max))

    # 4) K 线额度余额
    step("历史K线额度 (get_history_kl_quota)", lambda: quote.kline_quota(gw))

    # 5) 代码搜索
    step("代码搜索 (get_search_quote)", lambda: quote.search_quote(gw, args.code, max_count=3))

    # 6) 交易日历(校验代码转换与市场参数)
    step("交易日历 (request_trading_days)", lambda: quote.trading_days(gw, market=cfg.market))

    # 7) 交易侧(需交易登录)
    if args.trade:
        step("交易账户列表 (get_acc_list)", lambda: trade.accounts(gw))
        step("账户资金 (accinfo_query)", lambda: trade.funds(gw))
        step("持仓 (position_list_query)", lambda: trade.positions(gw))
        step("当日订单 (order_list_query)", lambda: trade.orders(gw))

    failed = [r for r in results if not r["ok"]]
    if args.json:
        print(json.dumps({"opend": f"{cfg.host}:{cfg.port}", "results": results},
                         ensure_ascii=False, indent=2, default=str))
    else:
        print("-" * 72)
        print(f"通过 {len(results) - len(failed)} / {len(results)}")
        if failed:
            print("失败项:")
            for r in failed:
                print(f"  - {r['step']}: {r['error']}")
    gw.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

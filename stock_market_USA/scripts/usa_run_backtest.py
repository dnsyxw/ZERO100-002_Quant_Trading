"""美股单次回测入口。

示例::

    # 先验默认配置(不需要先跑选参)
    python scripts/usa_run_backtest.py

    # 指定区间 / 持仓数 / 择时
    python scripts/usa_run_backtest.py --start 2005-01-01 --end 2014-12-31 --n 100 --ma 200

    # 读一份被选中的配置(由 usa_optimize.py 产出)
    python scripts/usa_run_backtest.py --cfg config/usa_best.json

    # 成本压力测试(全部费率 ×3)
    python scripts/usa_run_backtest.py --cost-mult 3

输出: `results/<时间戳>/` 下的 summary.json / cfg.json / nav.parquet / trades.parquet /
schedule.parquet。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]        # 本程序目录 (stock_market_USA/)
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from quant_common.paths import REPO_ROOT, resolve_input  # noqa: E402
from quant_usa import panels as uspanels  # noqa: E402
from quant_usa import presets as uspresets  # noqa: E402
from quant_usa.costs import USTradeCosts  # noqa: E402
from quant_usa.runner import run_us_strategy, save_us_result  # noqa: E402
from quant_usa.strategy import USStrategyConfig  # noqa: E402
from quant_usa.universe import USUniverseFilter  # noqa: E402


def _stdio() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace") if s.isatty() else s.reconfigure(
                encoding="utf-8", errors="replace")
        except Exception:
            pass


def _load_factor_set(name: str) -> dict[str, float]:
    """按名字取一份因子权重集(与 `usa_optimize.py` 的 `FACTOR_SETS` 同源)。

    这里用 importlib 按路径加载 `usa_optimize.py`(它不在包内, 是脚本),
    避免把因子集定义抄成两份 —— 抄两份必然漂移。
    """
    import importlib.util

    path = Path(__file__).with_name("usa_optimize.py")
    spec = importlib.util.spec_from_file_location("_usa_optimize", path)
    if spec is None or spec.loader is None:      # pragma: no cover - 环境异常
        raise SystemExit(f"[错误] 无法加载 {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sets = getattr(mod, "FACTOR_SETS", {})
    if name not in sets:
        raise SystemExit(f"[错误] 未知因子集 {name!r}; 可用: {', '.join(sets)}")
    return dict(sets[name])


def build_config(args) -> USStrategyConfig:
    if args.cfg:
        cfg = USStrategyConfig.load(args.cfg)
    else:
        cfg = USStrategyConfig()
        cfg.factor_w = dict(uspresets.US_FACTOR_W_PRIOR)
    # 显式传入的 CLI 参数覆盖配置文件
    if args.start:
        cfg.start = args.start
    if args.end:
        cfg.end = args.end
    if args.n:
        cfg.n_stocks = args.n
    if args.ma is not None:
        cfg.ma_window = args.ma
    if args.timing_index:
        cfg.timing_index = args.timing_index
    if args.vol_target is not None:
        cfg.vol_target = args.vol_target
    if args.dd_stop is not None:
        cfg.dd_stop = args.dd_stop
    if args.off_scale is not None:
        cfg.off_scale = args.off_scale
    if args.factor:
        cfg.factor_w = _load_factor_set(args.factor)
    u = cfg.universe
    if args.min_adv is not None:
        u.min_adv = args.min_adv
    if args.min_price is not None:
        u.min_price = args.min_price
    if args.min_age is not None:
        u.min_age_days = args.min_age
    if args.max_zero_vol is not None:
        u.max_zero_vol_ratio = args.max_zero_vol
    if args.max_mktcap is not None:
        u.max_mktcap = args.max_mktcap
    if args.min_mktcap is not None:
        u.min_mktcap = args.min_mktcap
    cfg.universe = u
    return cfg


def resolve_cfg_path(raw: str) -> Path:
    """解析配置路径; `usa_best.json` 不存在时回落到先验配置, 并明确告知。

    路径相对**当前工作目录**或**本程序目录**都接受 —— 这样双击启动器
    (cwd=仓库根, 传 `stock_market_USA/config/...`) 与直接进 `stock_market_USA/` 跑都能用。
    回落逻辑让"一键启动器"里的回测任务在用户还没跑选参之前也能直接用。
    """
    p = resolve_input(raw, ROOT, REPO_ROOT)
    if p.exists():
        return p
    fallback = ROOT / "config" / "usa_prior.json"
    if p.name == "usa_best.json" and fallback.exists():
        print(f"[提示] 未找到 {p.name}(还没跑选参), 自动改用文献先验配置 "
              f"stock_market_USA/config/usa_prior.json")
        print("       想要选参结果: 双击「美股·训练段选参 + 样本外验证」")
        return fallback
    raise SystemExit(f"[错误] 找不到配置文件: {p}")


def main() -> int:
    _stdio()
    ap = argparse.ArgumentParser(description="美股量化回测")
    ap.add_argument("--cfg", default=str(ROOT / "config" / "usa_best.json"),
                    help="配置文件路径(JSON)")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--n", type=int, default=0, help="持仓数量")
    ap.add_argument("--ma", type=int, default=None, help="择时均线窗口(0=不择时)")
    ap.add_argument("--timing-index", default=None, help=".INX / .IXIC / .DJI / SPY / QQQ / IWM")
    ap.add_argument("--vol-target", type=float, default=None, help="目标年化波动(0=关闭)")
    ap.add_argument("--dd-stop", type=float, default=None, help="指数回撤熔断阈值(0=关闭)")
    ap.add_argument("--off-scale", type=float, default=None, help="离场时保留仓位比例")
    ap.add_argument("--min-adv", type=float, default=None, help="日均成交额下限(美元)")
    ap.add_argument("--min-price", type=float, default=None, help="绝对价格下限(美元)")
    ap.add_argument("--min-age", type=int, default=None, help="上市最少交易日")
    ap.add_argument("--max-zero-vol", type=float, default=None, help="20日零成交占比上限")
    ap.add_argument("--max-mktcap", type=float, default=None, help="市值上限(0=不限)")
    ap.add_argument("--min-mktcap", type=float, default=None, help="市值下限(0=不限)")
    ap.add_argument("--factor", default=None,
                    help="因子集名(见 usa_optimize.py 的 FACTOR_SETS: prior/mom/lowvol/"
                         "max/mom_lowvol/rev_lowvol/small_rev)")
    ap.add_argument("--cost-mult", type=float, default=1.0, help="成本压力测试倍数")
    ap.add_argument("--cash", type=float, default=1_000_000.0, help="初始资金(美元)")
    ap.add_argument("--no-frame-cache", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cfg_path = resolve_cfg_path(args.cfg)
    args.cfg = str(cfg_path)
    cfg = build_config(args)
    costs = USTradeCosts().scaled(args.cost_mult) if args.cost_mult != 1.0 else USTradeCosts()
    print("配置: " + json.dumps(cfg.as_dict(), ensure_ascii=False))
    print(f"成本: {json.dumps(costs.as_dict(), ensure_ascii=False)}")
    print(f"股票池: {cfg.universe.describe()}")
    print()

    res = run_us_strategy(cfg, costs=costs, cache_frames=not args.no_frame_cache,
                          initial_cash=args.cash)
    out_dir = Path(args.out) if args.out else ROOT / "results" / datetime.now().strftime("%Y%m%d_%H%M%S")
    save_us_result(res, out_dir)
    m = res.metrics
    print()
    print(f"基准({m.get('benchmark', '-')}): 年化 {m.get('bench_annual_return', float('nan')):.2%}  "
          f"回撤 {m.get('bench_max_drawdown', float('nan')):.2%}  "
          f"超额 {m.get('excess_annual', float('nan')):+.2%}")
    print(f"目标(年化≥20% 且 回撤≤20%): {'✅ 达成' if m.get('target_met') else '❌ 未达成'}")
    print(f"结果已保存: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""一键启动器的"大脑" —— 中文菜单与任务分派。

为什么任务逻辑放在 Python 而不是 .bat
------------------------------------------------
`.bat` 有两个硬坑(均已实测):

1. **`chcp 65001` 会打断 cmd.exe 对批处理文件的解析** —— 在文件中间调用 `chcp` 后,
   同一文件后续的命令不再执行(纯 ASCII 文件也一样)。所以 .bat 里 **不能** 用 chcp;
2. 批处理文件自身的编码在 cp936 / UTF-8 之间没有两全解。

因此本项目约定: **.bat 只做"找到 Python + 调本脚本", 内容全为 ASCII;
所有中文输出由 Python 负责**(Python 能按目标流是否为控制台自动选择正确编码)。
文件编码与解析问题从此彻底消失, 中文又完全正常。

用法::

    python tools/launcher.py --menu          # 交互菜单(根目录 启动.bat 调它)
    python tools/launcher.py --task smoke    # 直接跑某个任务
    python tools/launcher.py --list          # 列出全部任务
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _setup_stdio() -> None:
    """让中文在两种环境下都正确, 且永不因编码崩溃。

    - 交互式控制台(双击 .bat): 保留系统编码(中文 Windows 是 cp936);
    - 管道/重定向(被别的程序捕获): 用 UTF-8。
    只看 isatty(), 不去动 chcp —— 这也是不需要 chcp 的原因。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


@dataclass(frozen=True)
class Task:
    """一个可一键启动的任务。"""

    key: str
    title: str
    script: str
    args: tuple[str, ...] = ()
    note: str = ""
    group: str = "其他"
    prompt: str = ""            # 非空则先询问用户输入
    prompt_flag: str = ""       # 用户输入以哪个选项名传给脚本(空则作为位置参数)


TASKS: tuple[Task, ...] = (
    Task("install_opend", "安装 Futu OpenD", "common/scripts/install_opend.py", ("--launch",),
         "下载最新版并启动安装向导; 装完需人工启动并登录", "富途 OpenAPI 接入"),
    Task("check_env", "富途环境自检", "common/scripts/check_futu_env.py", ("--deep",),
         "OpenD 连不上 / 报错时先跑这个", "富途 OpenAPI 接入"),
    Task("smoke", "富途冒烟测试", "common/scripts/futu_smoke.py", ("--trade",),
         "验证 连接→快照→K线→额度→搜索→日历→账户 整条链路", "富途 OpenAPI 接入"),
    Task("live_check", "富途全量自检", "common/scripts/futu_live_check.py", ("--trade",),
         "把所有只读接口逐个真跑一遍(含摆盘/逐笔/分时, 会自动订阅再释放)", "富途 OpenAPI 接入"),
    Task("mcp_selftest", "MCP 服务自检", "mcp/futu_server.py", ("--selftest",),
         "不连 OpenD, 只验证协议握手 / 工具注册 / stdout 洁净", "富途 OpenAPI 接入"),
    Task("fetch_docs", "更新富途官方文档", "common/scripts/fetch_futu_docs.py", (),
         "下载官方完整接口文档与 Skills 包到 _futu_raw/", "富途 OpenAPI 接入"),

    Task("download_data", "A股·下载行情数据", "stock_market_A/scripts/download_sina.py", ("--workers", "8"),
         "sina 批量源, 约 10 分钟, 支持断点续传", "A股量化模型"),
    Task("backtest", "A股·运行回测", "stock_market_A/scripts/run_backtest.py",
         ("--n", "80", "--q-lo", "0.0", "--q-hi", "0.35", "--min-amt", "3e7",
          "--ma", "60", "--timing-index", "000852.SH"),
         "防御型默认配置; 改参数请编辑 tools/launcher.py 的 TASKS", "A股量化模型"),
    Task("signal", "A股·生成月度调仓信号", "stock_market_A/scripts/build_monthly_signal.py", (),
         "需要输入决策日(月末最后交易日)", "A股量化模型",
         prompt="请输入决策日 YYYY-MM-DD (月末最后交易日): ", prompt_flag="--date"),
    Task("daily", "A股·每日盘后调度", "stock_market_A/scripts/run_daily.py", (),
         "纸面成交 / 择时闸门 / 风控留痕 / 净值快照", "A股量化模型"),
    Task("portfolio", "A股·查看当前组合", "stock_market_A/scripts/current_portfolio.py", (),
         "输出 stock_market_A/docs/05_当前组合.md 与 stock_market_A/results/current_portfolio/", "A股量化模型"),

    Task("hk_download", "港股·下载全市场数据", "stock_market_HK/scripts/hk_download_data.py",
         ("--workers", "10", "--no-turn"),
         "首次约 5-8 分钟(新浪直连); 可重复运行断点续传", "港股量化模型"),
    Task("hk_retry", "港股·补齐数据缺口", "stock_market_HK/scripts/hk_data_retry.py",
         ("--sleep", "20", "--rounds", "3"),
         "首次下载被新浪限频时用; 隔一段时间再跑即可继续补", "港股量化模型"),
    Task("hk_factor_ic", "港股·因子有效性检验", "stock_market_HK/scripts/hk_factor_ic.py",
         ("--start", "2015-01-01", "--end", "2021-12-31",
          "--out", "stock_market_HK/results/factor_ic_train.json"),
         "训练段 RankIC; 决定用哪些因子与方向", "港股量化模型"),
    Task("hk_optimize", "港股·训练段选参 + 样本外验证", "stock_market_HK/scripts/hk_optimize.py",
         ("--validate",),
         "只在训练段(2015-2021)选参数, 再跑样本外; 输出 stock_market_HK/config/hk_best.json", "港股量化模型"),
    Task("hk_diagnose", "港股·诊断与可达前沿扫描", "stock_market_HK/scripts/hk_diagnose.py", (),
         "扫池子域×因子×风控, 含随机选股/池子等权基准对照", "港股量化模型"),
    Task("hk_max_ann30", "港股·回撤≤30% 下的年化最大化", "stock_market_HK/scripts/hk_max_ann30.py",
         ("--initial-cash", "20000000", "--out", "stock_market_HK/results/max_ann30"),
         "目标改写版: 在最大回撤 ≤30% 的硬约束下求年化最高组合; "
         "网格含池子×因子×持仓数×调仓频率×择时×杠杆×杠铃权重, 约 40-60 分钟",
         "港股量化模型"),
    Task("hk_max_ann30_quick", "港股·回撤≤30% 快速试跑", "stock_market_HK/scripts/hk_max_ann30.py",
         ("--quick", "--initial-cash", "20000000", "--out", "stock_market_HK/results/max_ann30_quick"),
         "小网格(约 3 分钟), 用来确认环境与数据就绪; 结论请以完整版为准", "港股量化模型"),
    Task("hk_backtest", "港股·运行回测", "stock_market_HK/scripts/hk_run_backtest.py",
         ("--cfg", "stock_market_HK/config/hk_best.json"),
         "需先跑「港股·训练段选参」生成 stock_market_HK/config/hk_best.json", "港股量化模型"),
    Task("hk_backtest_prior", "港股·回测(先验配置)", "stock_market_HK/scripts/hk_run_backtest.py",
         ("--cfg", "stock_market_HK/config/hk_prior.json"),
         "不依赖选参结果, 用文献先验权重直接跑全周期", "港股量化模型"),
    Task("hk_signal", "港股·生成调仓信号", "stock_market_HK/scripts/hk_build_signal.py",
         ("--cfg", "stock_market_HK/config/hk_prior.json"),
         "按每手股数取整生成买卖清单; 需要输入决策日", "港股量化模型",
         prompt="请输入决策日 YYYY-MM-DD (港股月末最后交易日): ", prompt_flag="--date"),
    Task("hk_daily", "港股·每日盘后调度", "stock_market_HK/scripts/hk_run_daily.py",
         ("--cfg", "stock_market_HK/config/hk_prior.json"),
         "纸面成交 / 风控熔断 / 净值快照, 留痕到 stock_market_HK/runtime/hk_*", "港股量化模型"),

    Task("us_download", "美股·下载全市场数据", "stock_market_USA/scripts/usa_download_data.py",
         ("--max-names", "4000", "--workers", "5", "--pause", "0.05"),
         "首次约 10-30 分钟(新浪直连, 含退市股); 被限频时改用 --workers 2 --pause 0.4 重跑续传",
         "美股量化模型"),
    Task("us_factor_ic", "美股·因子有效性检验", "stock_market_USA/scripts/usa_factor_ic.py",
         ("--start", "2005-01-01", "--end", "2014-12-31",
          "--out", "stock_market_USA/results/factor_ic_train.json"),
         "训练段 RankIC; 决定用哪些因子与方向", "美股量化模型"),
    Task("us_optimize", "美股·训练段选参 + 样本外验证", "stock_market_USA/scripts/usa_optimize.py",
         ("--validate",),
         "只在训练段(2005-2014)选参数, 再跑样本外; 输出 stock_market_USA/config/usa_best.json",
         "美股量化模型"),
    Task("us_diagnose", "美股·诊断与可达前沿扫描", "stock_market_USA/scripts/usa_diagnose.py",
         ("--start", "2005-01-01", "--end", "2014-12-31"),
         "扫池子域×因子×风控, 含随机选股/池子等权/SPY-QQQ-IWM 基准对照", "美股量化模型"),
    Task("us_backtest", "美股·运行回测", "stock_market_USA/scripts/usa_run_backtest.py",
         ("--cfg", "stock_market_USA/config/usa_best.json"),
         "需先跑「美股·训练段选参」生成 stock_market_USA/config/usa_best.json", "美股量化模型"),
    Task("us_backtest_prior", "美股·回测(先验配置)", "stock_market_USA/scripts/usa_run_backtest.py",
         ("--cfg", "stock_market_USA/config/usa_prior.json"),
         "不依赖选参结果, 用文献先验权重直接跑全周期", "美股量化模型"),
    Task("us_signal", "美股·生成调仓信号", "stock_market_USA/scripts/usa_build_signal.py",
         ("--cfg", "stock_market_USA/config/usa_prior.json"),
         "按整数股生成买卖清单; 需要输入决策日", "美股量化模型",
         prompt="请输入决策日 YYYY-MM-DD (美股月末最后交易日): ", prompt_flag="--date"),
    Task("us_daily", "美股·每日盘后调度", "stock_market_USA/scripts/usa_run_daily.py",
         ("--cfg", "stock_market_USA/config/usa_prior.json"),
         "纸面成交 / 风控熔断 / 净值快照, 留痕到 stock_market_USA/runtime/us_*", "美股量化模型"),

    Task("research_env", "个股调研·环境自检", "common/scripts/stock_research.py", ("env",),
         "调研个股前先跑; 验证行情/财报/公告源是否可达", "个股调研"),
    Task("research_price", "个股调研·价格路径", "common/scripts/stock_research.py", ("price",),
         "自动检测除权断层并复权后算回撤/相对表现", "个股调研",
         prompt="请输入股票代码 (如 sz002594 / sh688795 / sh000688): "),
    Task("research_skill", "个股调研·skill 位置", "common/scripts/stock_research.py", ("paths",),
         "确认 stock-analysis skill 装在哪 (排查转发失败用)", "个股调研"),

    # ---------------- 多资产组合(第 4 套模型: 跨资产类别趋势跟踪) ----------------
    Task("gl_download", "多资产·下载 ETF 数据", "stock_market_GLOBAL/scripts/gl_download_data.py",
         ("--workers", "5"),
         "Yahoo 总收益日线(含分红), 15 只 ETF + 现金腿; 断点续传", "多资产组合"),
    Task("gl_backtest", "多资产·运行回测", "stock_market_GLOBAL/scripts/gl_run_backtest.py",
         ("--md",),
         "用推荐配置跑全周期回测: 分段指标 / 逐年收益 / 最深回撤", "多资产组合"),
    Task("gl_signal", "多资产·生成调仓信号", "stock_market_GLOBAL/scripts/gl_build_signal.py",
         (),
         "算出**今天**应该持有的目标权重(含趋势分数与参考价)", "多资产组合"),
    Task("gl_orders_plan", "多资产·下单计划(演练)", "stock_market_GLOBAL/scripts/gl_place_orders.py",
         (),
         "只打印计划与账户闸门结果, **不发任何指令**; 真要下单需再加 --execute", "多资产组合"),
    Task("gl_scan", "多资产·研究扫描(全量)", "stock_market_GLOBAL/scripts/gl_scan.py",
         (),
         "基准对照/分层分解/风险刻度/参数面/危机窗口, 生成 docs/12 的全部表格; 约 10 分钟",
         "多资产组合"),
    Task("gl_scan_quick", "多资产·研究扫描(快速)", "stock_market_GLOBAL/scripts/gl_scan.py",
         ("--quick",),
         "只跑基准/分层/风险刻度三张表, 约 1 分钟", "多资产组合"),
    Task("gl_probe", "多资产·数据源探针", "stock_market_GLOBAL/scripts/gl_probe_sources.py",
         ("--sina",),
         "复核'为什么本程序用 Yahoo 而不是新浪'(ETF 总收益覆盖 vs 新浪缺口)", "多资产组合"),

    Task("tests", "运行全部测试", "tools/run_tests.py", (),
         "共享层/引擎/因子/策略/风控/纸面/富途/MCP/港股/美股/多资产/启动器", "质量"),
)

BY_KEY = {t.key: t for t in TASKS}


def _banner(title: str, note: str = "") -> None:
    line = "=" * 76
    print(line)
    print(f"  {title}")
    if note:
        print(f"  {note}")
    print(line)
    print(f"  项目目录: {ROOT}")
    print()


def run_task(task: Task, extra_args: list[str] | None = None) -> int:
    """执行一个任务; 返回子进程退出码。"""
    script = ROOT / task.script
    if not script.exists():
        print(f"[失败] 找不到脚本: {script}")
        return 1
    args = list(task.args)
    if extra_args:
        args += extra_args
    cmd = [sys.executable, str(script), *args]
    _banner(task.title, task.note)
    print("[执行] " + " ".join(cmd))
    print("-" * 76)
    code = subprocess.call(cmd, cwd=str(ROOT))
    print("-" * 76)
    print()
    print(f"[完成] {task.title}" if code == 0 else f"[失败] {task.title} —— 退出码 {code}")
    if code != 0:
        print("       把上面的报错信息发给 AI 即可定位。")
    return code


def task_with_prompt(task: Task) -> int:
    """需要用户输入的任务(如决策日)。"""
    try:
        value = input(task.prompt).strip()
    except (EOFError, KeyboardInterrupt):
        value = ""
    if not value:
        print("[取消] 未输入内容, 已退出。")
        return 1
    extra = [task.prompt_flag, value] if task.prompt_flag else [value]
    return run_task(task, extra)


def menu() -> int:
    """交互式菜单; 返回最后一个任务的退出码(用户选 0 退出则返回 0)。"""
    while True:
        line = "=" * 76
        print()
        print(line)
        print("  lianghua 量化项目 —— 一键启动")
        print(line)
        print()
        groups: list[str] = []
        for task in TASKS:
            if task.group not in groups:
                groups.append(task.group)
        index: dict[str, Task] = {}
        counter = 0
        for group in groups:
            print(f"  [{group}]")
            for task in TASKS:
                if task.group != group:
                    continue
                counter += 1
                index[str(counter)] = task
                print(f"   {counter:2d}. {task.title}  ({task.note})")
            print()
        print("     0. 退出")
        print()
        print("-" * 76)
        try:
            choice = input("请输入序号后回车: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice in ("", "0"):
            return 0
        task = index.get(choice)
        if task is None:
            print(f"[提示] 无效的序号: {choice}")
            continue
        if task.prompt:
            task_with_prompt(task)
        else:
            run_task(task)
        try:
            input("\n按回车返回菜单 ...")
        except (EOFError, KeyboardInterrupt):
            return 0


def main(argv: list[str] | None = None) -> int:
    _setup_stdio()
    ap = argparse.ArgumentParser(description="lianghua 一键启动器")
    ap.add_argument("--menu", action="store_true", help="交互式菜单")
    ap.add_argument("--task", help="直接执行的任务 key")
    ap.add_argument("--list", action="store_true", help="列出全部任务")
    ap.add_argument("--args", nargs=argparse.REMAINDER, default=[], help="追加给任务的参数")
    args = ap.parse_args(argv)

    if args.list:
        for task in TASKS:
            print(f"{task.key:16s} {task.group:12s} {task.title}  ->  {task.script} {' '.join(task.args)}")
        return 0
    if args.task:
        if args.task == "menu":          # `_common.bat menu` → 交互菜单
            return menu()
        task = BY_KEY.get(args.task)
        if task is None:
            print(f"[失败] 未知任务: {args.task}")
            print("可用任务: " + ", ".join(BY_KEY))
            return 1
        if task.prompt and not args.args:
            return task_with_prompt(task)
        return run_task(task, list(args.args))
    return menu()


if __name__ == "__main__":
    raise SystemExit(main())

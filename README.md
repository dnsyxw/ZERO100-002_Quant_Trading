# lianghua — 本地量化自动交易项目（A股 + 港股 + 美股 + 多资产组合）

基于权威文献/研报方法论（见 `stock_market_A/docs/01_方法论调研.md`、
`stock_market_HK/docs/08_港股方法论调研.md`、`stock_market_USA/docs/10_美股方法论调研.md`、
`stock_market_GLOBAL/docs/12_多资产趋势组合.md`），
以真实历史行情构建的 **四套并列的市场模型**：

| 市场 | 数据源 | 模型 | 文档 |
|---|---|---|---|
| **A股** | baostock + sina/AKShare（2014-01 至今，含退市股、逐日ST/停牌标记） | 中小市值多因子选股（月度调仓）+ 指数均线择时 | `stock_market_A/docs/01`–`06` |
| **港股** | 新浪直连（自研解码，含退市股历史）+ 富途 OpenD 元数据 | 港股通域多因子选股（月度调仓）+ 均线/波动率目标/回撤熔断 | `stock_market_HK/docs/08`–`09` |
| **美股** | 新浪美股直连（**含退市股历史**，2004 至今）+ 富途 OpenD 上市日期 | 全市场多因子选股（月度调仓）+ 均线/波动率目标/回撤熔断 | `stock_market_USA/docs/10`–`11` |
| **多资产组合** | Yahoo chart **总收益 adjclose**（含分红，1993 至今）+ 富途 OpenD | **跨 8 个资产类别 15 只 ETF 的时序趋势跟踪**（月度调仓）+ 等风险 / 波动率目标 / 回撤调速器 | `stock_market_GLOBAL/docs/12` |
| **共享** | 富途 OpenD（行情 + 交易） | 券商接入层 + 29 个 MCP 工具 | `common/docs/07` |

四套模型配套 **自动信号生成 → 风控 → 纸面成交** 的自动交易程序，并共用
"过滤 → 因子 → 打分 → 择时 → 引擎 → 绩效" 的分层与方法论口径。

> ⚠️ 本仓库为**研究/学习项目，非投资建议**。回测达标 ≠ 未来收益；
> 自动下单需自备券商 QMT/miniQMT（A股）或富途等合规通道并遵守程序化交易报备要求。
> 设计取舍与自问自答见 `stock_market_A/docs/02_设计决策QA.md`。

## 远端仓库与日常同步（私有库）

代码托管在 GitHub 私有库：<https://github.com/dnsyxw/ZERO100-002_Quant_Trading>
（分支 `main`，本地 `main` 已跟踪 `origin/main`）。

**日常只需要双击，不用敲命令：**

| 要做什么 | 双击 |
|---|---|
| 把本地改动提交并推送上去 | `启动.bat` → 「同步·推送到 GitHub」，或 `launcher\42-同步·推送到GitHub.bat` |
| 把 GitHub 上的更新拉回本地 | `启动.bat` → 「同步·从 GitHub 拉取」，或 `launcher\43-同步·从GitHub拉取.bat` |
| 只看状态（不动任何文件） | `launcher\44-同步·查看同步状态.bat` |
| 第一次用 / 凭据失效了 | `launcher\45-同步·登录GitHub.bat` |

同步逻辑的唯一真源是 `tools/git_sync.py`（用 `tools/launcher.py` 的 `TASKS` 注册任务）。
它刻意**不做**这些事，以免帮倒忙：

- 远端有本地没有的提交时**拒绝推送**（绝不 `--force` 覆盖别人的提交）；
- 本地有未提交改动时**拒绝拉取**（绝不自动 `stash` 把改动藏起来）；
- 不做自动 rebase、不做自动冲突合并。

> `runtime/`、各市场的 `data/*cache/`、`pylibs/`、`_futu_raw/` 都在 `.gitignore` 里，
> 属于运行态或可重建的下载产物，**不会**被推上去。唯一破例的是
> `stock_market_GLOBAL/results/`（约 2 MB 回测产物，作为第 4 套程序的结论证据）。

## 目录结构

**两套程序各自独立成目录，互不 import，可分别开发、分别修改、分别跑测试。**

```
lianghua/
├── 启动.bat               双击入口: 中文菜单（唯一入口）
├── launcher/              每任务一个 .bat，均可单独双击
├── tools/                 启动器基础设施（跨程序）
│   ├── launcher.py        ★ 任务清单唯一真源 TASKS（加任务只改这里）
│   ├── make_launcher.py   按 TASKS 重新生成 .bat
│   └── run_tests.py       跑四个测试根
├── common/                ── 共享层（市场无关，两个程序都依赖）
│   ├── quant_common/
│   │   ├── paths.py       仓库路径单一真源（别再写 parents[N]）
│   │   ├── metrics.py     绩效指标（年化/回撤/夏普/Calmar）
│   │   ├── scoring.py     截面打分与 Top-N 选股
│   │   ├── preprocess.py  MAD 去极值 / z-score
│   │   └── futu/          富途 OpenD 网关（连接/行情/交易/硬风控）
│   ├── config/futu.json   共享配置（不含任何密码）
│   ├── scripts/           富途自检、官方文档下载、个股调研
│   └── docs/07_富途OpenAPI接入.md
├── stock_market_A/        ── A股量化程序（自成一体）
│   ├── quant_a/
│   │   ├── core/       撮合引擎(T+1/涨跌停/整手/先卖后买) + A股成本模型
│   │   ├── data/       baostock 源、本地缓存、因子矩阵、引擎面板
│   │   ├── factors/    量价因子（截面预处理在共享层）
│   │   ├── strategy/   股票池过滤、打分选股、指数择时、决策构建
│   │   ├── backtest/   端到端 runner
│   │   └── autotrade/  订单生成、纸面记账、硬风控
│   ├── scripts/  tests/  config/  docs/01–06
│   └── data/cache/  results/  runtime/  orders/
├── stock_market_HK/       ── 港股量化程序（自成一体）
│   ├── quant_hk/
│   │   ├── codes.py    港股代码规范、GEM/柜台后缀判定、每手股数兜底
│   │   ├── store.py    港股本地缓存(stock_market_HK/data/hk_cache/, 与A股缓存分开)
│   │   ├── source.py   新浪直连解码(线程安全解码器池) + 腾讯 + 富途元数据
│   │   ├── factors.py  短期反转/低波/Amihud非流动性/量比/成交额
│   │   ├── universe.py 仙股清洗、GEM剔除、流动性/价格下限
│   │   ├── timing.py   均线闸门 + 波动率目标 + 回撤熔断
│   │   ├── strategy.py 决策构建（打分/选股复用共享层）
│   │   ├── engine.py   港股引擎(每手股数各异 / 无涨跌停 / 先卖后买)
│   │   ├── costs.py    印花税双边0.1%向上取整 / 交易费 / 征费 / 结算费
│   │   └── runner.py   端到端回测 + prepare_frames(参数扫描复用矩阵)
│   ├── scripts/  tests/  config/  docs/08–09
│   └── data/hk_cache/  results/  runtime/  orders/
├── stock_market_GLOBAL/   ── 多资产组合程序（自成一体，第 4 套）
│   ├── quant_global/       universe / source / store / signals / allocate /
│   │                       engine / backtest / report / config
│   ├── scripts/  tests/  config/  docs/12
│   └── data/gl_cache/  results/
├── mcp/                   富途工具集(29 个工具)，由 DSH 的 dsh-mcp-client 拉起
│   ├── mcp_stdio.py       零依赖 MCP stdio 服务端框架
│   └── futu_server.py     ⚠ 路径被 DSH 外部配置写死，不要移动
├── pylibs/                仓库内依赖（cp310 轮子）
└── runtime/               共享运行态（富途日志与下单留痕）
```

> **为什么共享层不是"第三套程序"**：`metrics`/`scoring` 是纯数学，两边必须同口径结果才可比；
> `futu` 是券商接入 + 下单风控，拷两份等于制造两份会漂移的连接与风控代码。
> 除这三样之外的一切（成本模型、撮合引擎、因子、股票池、择时）两边机制不同，**各写各的**。
> 回归测试 `tools/tests/test_launcher.py::Test程序隔离` 守着"A股不 import 港股、反之亦然"。

### 旧路径 → 新路径对照

2026-09 做过一次目录重构（把两套模型拆成互不依赖的独立程序）。如果你记得旧路径，按下表找：

| 旧 | 新 |
|---|---|
| `quant_lh/{core,data,factors,strategy,backtest,autotrade}/` | `stock_market_A/quant_a/` 同名子包 |
| `quant_lh/core/metrics.py` | `common/quant_common/metrics.py` |
| `quant_lh/strategy/scoring.py` | `common/quant_common/scoring.py` |
| `quant_lh/factors/preprocess.py` | `common/quant_common/preprocess.py` |
| `quant_lh/futu/` | `common/quant_common/futu/` |
| `quant_lh/hk/` | `stock_market_HK/quant_hk/` |
| `scripts/<A股脚本>.py` | `stock_market_A/scripts/<同名>` |
| `scripts/hk_*.py` | `stock_market_HK/scripts/hk_*.py` |
| `scripts/{check_futu_env,futu_smoke,futu_live_check,install_opend,fetch_futu_docs,stock_research}.py` | `common/scripts/` |
| `scripts/{launcher,make_launcher,run_tests}.py` | `tools/` |
| `tests/`（除下面三类） | `stock_market_A/tests/` |
| `tests/futu/` | `common/tests/futu/` |
| `tests/hk/` | `stock_market_HK/tests/` |
| `tests/test_launcher.py` | `tools/tests/test_launcher.py` |
| `config/hk_*.json` | `stock_market_HK/config/` |
| `config/futu.json` | `common/config/futu.json` |
| `config/{best_strategy,target_*}.json` | `stock_market_A/config/` |
| `docs/01`–`06` | `stock_market_A/docs/` |
| `docs/07_富途OpenAPI接入.md` | `common/docs/` |
| `docs/08`–`09` | `stock_market_HK/docs/` |
| `data/cache/` | `stock_market_A/data/cache/` |
| `data/hk_cache/` | `stock_market_HK/data/hk_cache/` |
| `results/hk/` | `stock_market_HK/results/` |
| `results/`（其余） | `stock_market_A/results/` |
| `runtime/{paper_*,pending_orders.json}` | `stock_market_A/runtime/` |
| `runtime/hk_*` | `stock_market_HK/runtime/` |
| `runtime/{futu_*,opend_download}` | `runtime/`（不变，共享） |
| `orders/` | `stock_market_A/orders/`（港股是 `stock_market_HK/orders/`） |
| `mcp/` | **不变**（DSH 外部配置写死了这个路径，挪了 `mcp__futu__*` 工具会全失效） |

## 多资产组合模型（第 4 套，与前三个模型**方法论上不同**）

前三套模型走的都是「单市场纯多头 + 截面选股」，三次都没达成"年化≥20% 且 回撤≤20%"，
而且失败方式完全相同（复盘见 `stock_market_GLOBAL/docs/12` §1）。第 4 套程序**换掉了问题本身**：

| 维度 | A股/港股/美股 | 多资产组合 |
|---|---|---|
| 收益来源 | 猜"哪只涨得多"（截面 alpha） | 只判断"它在涨还是在跌"（**时序趋势**） |
| 分散维度 | 同一市场内的几十~几百只股票 | **8 个资产类别、15 只 ETF** |
| 回撤控制 | 外生指数均线闸门 / 熔断 | **逐资产止损 + 波动率目标 + 组合自身回撤调速器** |
| 数据源 | 新浪 / baostock | **Yahoo 总收益 adjclose（含分红，1993 至今）** |
| 杠杆 | 不用 | ≤2.0 倍（波动率目标自动调节） |

**实测（2003-01 ~ 2026-09，24 年）**：年化 **13.19%**、最大回撤 **25.60%**、夏普 **0.91**、
**24 年只有 2 个亏损年**（最差 −6.8%）；同期标普500 是 11.41% / 55.19% / 0.68 / 最差 −37.0%。
**2008 年 +9.1%、2022 年 +0.4%**。

### 快速开始（多资产组合）

> **双击 `启动.bat`**，选「多资产组合」组。全部 6 个任务都能在
> `launcher\34-多资产·下载ETF数据.bat` ~ `launcher\39-多资产·数据源探针.bat` 单独双击。

```powershell
$py = "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe"
$env:PYTHONPATH="$PWD\pylibs;$PWD\common;$PWD\stock_market_GLOBAL"
$env:NO_PROXY='*'; $env:no_proxy='*'

& $py stock_market_GLOBAL\scripts\gl_download_data.py --workers 5      # 1) 数据(~1 分钟)
& $py stock_market_GLOBAL\scripts\gl_run_backtest.py --md              # 2) 回测
& $py stock_market_GLOBAL\scripts\gl_build_signal.py --equity 1000000 --orders   # 3) 当前信号
& $py stock_market_GLOBAL\scripts\gl_scan.py                           # 4) 全部研究表格(~10 分钟)
```

> ⚠️ **如实结论**：这套组合的目标是"**亏损可控、不存在腰斩风险**"，它做到了历史最大回撤 25.6%；
> 但**它不是 20% 年化**（13.2%，或提高杠杆到 14.7%）。它的失败模式是「长期磨」（2015-01 那次
> 用了 **856 天**才创新高）与「牛市跑输」（2023 年只赚 1.3%）。
> 另外**没有真正的样本外验证**（设计过程看了全样本），缓解办法是报整张参数面而不是最优点 ——
> 完整偏差清单见 `stock_market_GLOBAL/docs/12` §6。

## 美股模型（与 A 股/港股并列的第三套市场实现）

美股在**交易机制、成本结构、股票池陷阱、数据可得性**四方面与前两个市场都不同，
因此 `stock_market_USA/quant_usa/` 是一套独立实现（差异清单见 `quant_usa/__init__.py`）：

| 维度 | A 股 | 港股 | 美股 |
|---|---|---|---|
| 每手股数 | 固定 100 股 | 按股各异（20~100000） | **1 股**（无整手概念） |
| 涨跌停 | ±10%/20%/5% | 无（仅 VCM） | 无（仅 LULD 熔断，不阻止成交） |
| 印花税 | 卖出 0.05% | 双边 0.1%（向上取整） | **无** |
| 主要成本 | 佣金+过户费 | 印花税+交易费+征费+结算费 | **SEC 规费+TAF（仅卖出）**，往返纯规费约 0.0028% |
| 成本真正的构成 | 佣金 | 印花税 | **滑点**（规费几乎可忽略） |
| 股票池陷阱 | ST/退市/次新 | 仙股/老千股/GEM | **SPAC / OTC粉单 / penny stock** |
| 中期动量 | 弱（IC≈0） | 略强但不显著 | **训练段实测反向**（见下） |
| 退市率 | 中 | 中 | **高**（纳斯达克 6-8%/年）→ 幸存者偏差最严重 |

### 快速开始（美股）

> **双击 `启动.bat`**，选「美股量化模型」组。全部 8 个美股任务都能在
> `launcher\23-美股·下载全市场数据.bat` ~ `launcher\30-美股·每日盘后调度.bat` 单独双击。

```powershell
$py = "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe"
$env:PYTHONPATH="$PWD\pylibs"; $env:NO_PROXY='*'; $env:no_proxy='*'

# 1) 数据（首次 10-30 分钟；含清单/指数/全市场日线/快照，可重复运行断点续传）
#    被新浪限频(HTTP 456)时改用: --workers 2 --pause 0.4 --skip-registry --skip-index
& $py stock_market_USA\scripts\usa_download_data.py --max-names 4000 --workers 5

# 2) 因子有效性检验（只在训练段 2005-2014）
& $py stock_market_USA\scripts\usa_factor_ic.py --start 2005-01-01 --end 2014-12-31

# 3) 可达前沿扫描（86 组，含随机选股/池子等权/指数基准对照）
& $py stock_market_USA\scripts\usa_diagnose.py --start 2005-01-01 --end 2014-12-31

# 4) 训练段选参 + 样本外验证（336 组，约 1 小时）
& $py stock_market_USA\scripts\usa_optimize.py --validate

# 5) 回测 / 调仓信号 / 每日盘后调度（纸面）
& $py stock_market_USA\scripts\usa_run_backtest.py --cfg stock_market_USA\config\usa_best.json
& $py stock_market_USA\scripts\usa_build_signal.py --cfg stock_market_USA\config\usa_best.json --date 2026-08-31
& $py stock_market_USA\scripts\usa_run_daily.py --cfg stock_market_USA\config\usa_best.json
```

**必须由人工完成**（代码不代做）：
1. 确认标的在券商**可交易范围**内（部分中概/小盘需签风险协议或根本不可买）；
2. 确认是否临近**财报日**（本项目不做财报日历，事件风险自担）；
3. 美股有**盘前/盘后**交易，价差远大于盘中 —— 建议只在盘中下限价单，避开开盘 5 分钟。

### 美股数据源的工程结论（重要）

实测（2026-09）：**本机只有新浪美股直连可用**，其余源逐条否证。

| 源 | 实测 | 覆盖 | 关键字段 |
|---|---|---|---|
| **新浪美股日K** | ✅ 100%，0.036s/只（10线程） | **含已退市股** | OHLCV |
| **新浪美股清单** | ✅ 20条/页×906页 | 6,320 条 → 清洗后 **3,363 只** | 名称/交易所/市值/PE |
| **新浪实时快照** | ✅ ~1s/批（36字段） | 当前在册 2,311 只 | 真实价/**总股本**/52周高低 |
| Yahoo chart | ⚠️ `requests` 403；`curl_cffi` 伪装可通，但**退市股全部 404** | 仅当前在册 | raw+adjclose+拆股/分红事件 |
| Stooq | ❌ JS 挑战页 | — | — |
| 东财 `push2his` | ❌ 连接被重置 | — | — |

- **选新浪的决定性理由是退市股覆盖**：实测 27 个已退市代码（TWTR/SIVB/FRC/ATVI/VMW/XLNX/PXD…），
  Yahoo **全部 404**，新浪**全部有完整历史**。用 Yahoo 建池 = 只回测活到今天的公司，
  而美股退市率远高于 A 股/港股。代价是新浪序列**不做分红调整**（个股收益低估约 1.3-2.0%/年）。
- **新浪美股序列是「原生未复权价」**（三条独立判据：与 Yahoo raw 的比值分批常数、
  最新行与实时快照逐点相等、序列上能看到真实拆股阶跃）→ 必须做**拆股还原**，
  否则序列里会留着 −85%/−90%/+677% 的假跳变，反转/低波因子会把 10 拆 1 的股票
  当成"刚暴跌 90% 的深度超跌股"而重仓买入。
- **拆股还原判据经标定**：`prev_close/open` 与 `prev_close/close` **双条件 + ±3.5% 容差** ——
  23 个已知拆股召回 15、**42 个已知真实崩盘零误判**（FRC −49.4%、SIVB −60.4% 等）。
  关键在 `open`：拆股当日开盘价就按新价定，而 `close` 会被当日真实涨跌污染。
- **成交额只能用 `close × volume`**，不能用新浪的 `amount`（只在近期有值）。
  它是拆股还原中的**不变量**（价格÷k、股数×k），既是容量分析要的量，也是学术标准口径。
- **新浪会限频（HTTP 456）且全站生效**。脚本已内置节流 + 对 456 单独长退避 + 连续失败熔断；
  被限频时用 `--workers 2 --pause 0.4` 重跑（断点续传）。

> ⚠️ **如实结论**：美股侧"年化≥20% 且 最大回撤≤20%"**在训练段与样本外均未达成**。
> - **训练段（2005-2014）336 组配置中 124 组满足回撤≤20%（最低 14.09%），但年化最高的只有 12.84%**；
>   选定配置是 **N50 / 日均成交额≥1000万 / 小市值因子 / 波动率目标15% / MA150
>   → 年化 12.84%、回撤 17.45%、夏普 0.89、Calmar 0.74**。
> - **样本外（2015-01 起）年化 4.30%、回撤 44.87%**；6 个候选 **0/6 双达标**，
>   中位年化 10.67%、中位回撤 48.68%。
> - **86 组可达前沿扫描 0/86 双达标**；零成本的"池子等权全持有"上限是 9.46%/55.48%（训练段）、
>   8.22%/41.79%（样本外）——**它跑赢了绝大多数因子配置**。
> - **结构性原因**：同期标普500 自身回撤就是 33.9%–56.8%。把回撤压到 20% 必须大幅降仓/离场，
>   而降仓/离场会把年化压到 8–13%。**20% 回撤这条线实际等价于"把收益天花板锁在 8–13%"**。
> - **最有价值的失败记录**：① 动量在 2005-2014 **反向有效**（`mom_12_1+` 7.31% vs
>   `mom_12_1-` 9.02%），用数据推翻了 Jegadeesh-Titman 先验；② 均线闸门**挡得住急跌
>   （2008）挡不住阴跌（2022）** —— 样本外最大回撤那段（2021-02→2023-03，−44.87%）
>   闸门有 **60%** 的时间仍在持仓；③ **指数回撤熔断对小盘组合失效**（2022 指数只跌 25.4%，
>   组合跌 44.87%，熔断根本不触发）。
>
> 完整 RankIC 表、可达前沿、基准对照与机理诊断见 `stock_market_USA/docs/11_美股回测报告.md`。

## 港股模型（与 A 股并列的第二套市场实现）

港股与 A 股在**交易机制上根本不同**，直接复用 A 股引擎会在回测里引入系统性高估，
因此 `stock_market_HK/quant_hk/` 是一套独立实现（差异清单见 `stock_market_HK/quant_hk/__init__.py`）：

| 维度 | A 股 | 港股 |
|---|---|---|
| 每手股数 | 固定 100 股 | **按股票各异**（腾讯100/汇丰400/长和500，有的上万） |
| 涨跌停 | ±10%/20%/5% 硬约束 | **无涨跌停**（仅 VCM 冷静期）→ 单日 -90% 会真实进净值 |
| 印花税 | 卖出单边 0.05% | **买卖双边各 0.1%**，且**向上取整到 1 港元** |
| 股票池陷阱 | ST/退市/次新 | **仙股/老千股**（占数量50%+、成交额<5%）、GEM、`-R` 人民币柜台 |
| 小市值溢价 | 显著（壳价值） | **弱/反向**（SIZE 因子实测年化 −2.0%） |
| 低流动性溢价 | 强 | **方向相反**（流动性越好越优） |

### 快速开始（港股）

> **双击 `启动.bat`**，选「港股量化模型」组。全部 9 个港股任务都能在
> `launcher\12-港股·下载全市场数据.bat` ~ `launcher\20-港股·每日盘后调度.bat` 单独双击。

```powershell
$py = "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe"
$env:PYTHONPATH="$PWD\pylibs"; $env:NO_PROXY='*'; $env:no_proxy='*'

# 1) 数据（首次 5-8 分钟；含元数据/指数/全市场日线，可重复运行断点续传）
& $py stock_market_HK\scripts\hk_download_data.py --workers 10 --no-turn

# 2) 因子有效性检验（只在训练段 2015-2021）
& $py stock_market_HK\scripts\hk_factor_ic.py --start 2015-01-01 --end 2021-12-31

# 3) 训练段选参 + 样本外验证
& $py stock_market_HK\scripts\hk_optimize.py --validate

# 4) 回测
& $py stock_market_HK\scripts\hk_run_backtest.py --cfg stock_market_HK\config\hk_best.json

# 5) 调仓信号 / 每日盘后调度（纸面）
& $py stock_market_HK\scripts\hk_build_signal.py --cfg stock_market_HK\config\hk_best.json --date 2026-08-31
& $py stock_market_HK\scripts\hk_run_daily.py --cfg stock_market_HK\config\hk_best.json
```

**必须由人工完成**（代码不代做）：
1. 启动并登录 **Futu OpenD**（用于拉每手股数/上市日期；不登录也能跑，但整手约束会用默认值）；
2. 确认标的的**港股通资格**（非港股通标的无法用境内账户买入）；
3. 确认次日是否**台风/黑色暴雨休市**（港股特有，代码不预测天气）。

### 港股数据源的工程结论（重要）

实测（2026-09）：**腾讯源与东财源在本机被拒**，新浪可用且**保留退市股历史**。

- 主源 = **新浪直连**（自研）：原始 K 线是自定义压缩+字母表混淆，只能用它自己的 JS 解码器解；
  **V8 隔离池不可并发初始化，多线程会直接把进程 abort**（不是抛异常）。
  本项目用「解码器池（每实例一把锁）+ HTTP 10 线程」实测 **0.062s/只**，
  比 akshare 的 `stock_hk_daily`（0.75-1.0s/只，且会丢掉 `amount` 字段）**快 12 倍**。
- **港股没有免费的历史股本/市值时间序列**（东财被拒、腾讯被拒、亿牛空表、新浪日线不含
  `outstanding_share`）→ 本模型用 **绝对流动性下限 + 价格下限**代替 A 股的"市值分位"，
  并把 `adtv_log`/`illiq_20` 作为规模与流动性代理。取舍与理由见 `stock_market_HK/docs/08` §5。

> ⚠️ **如实结论**：港股侧"年化≥20% 且 最大回撤≤20%"**在训练段与样本外均未达成**。
> - 训练段（2015–2021）144 组配置中 113 组满足回撤≤20%，但**没有一组年化达到 20%**；
>   选中的配置是 **N40 / 日均成交额≥1亿 / 高非流动性 / 波动率目标10% / HSI MA120
>   → 年化 7.76%、回撤 18.74%**。
> - 样本外（2022-01 起）**年化 −1.53%、回撤 16.70%**。
> - 训练段唯一超过 20% 的配置（`illiq_20` 正权重，年化 **36%**）经检验是**流动性幻觉**：
>   把日均成交额下限从 2000 万提到 5000 万，年化立刻掉到 **7.6%** —— 它买的是
>   2000-3000 万成交额的 5-8 港元低价小盘股，容量上不成立。**本项目不予采用。**
>
> 值得注意的正向发现：**港股"等权持有整个流动股票池（零成本）"年化 10.82%、回撤仅 14.43%、
> 夏普 0.79**，跑赢恒生指数 11 个百分点且回撤只有其一半 —— 而**所有因子策略都没能跑赢它**。
>
> 完整可达前沿、因子 IC 表、流动性幻觉检验与失败原因见 `stock_market_HK/docs/09_港股回测报告.md`。

## 快速开始（A 股）

> **不想记命令？双击项目根目录的 `启动.bat`** —— 中文菜单列出全部任务，选序号即可。
> 每个任务也能在 `launcher\` 下单独双击运行。新增任务见 `launcher/README.md`。

```powershell
# 1) 依赖(本项目把依赖装在仓库内 pylibs/, 不污染系统环境)
#    注意: pylibs 内是 cp310 轮子(numpy2.2.6/pandas2.3.3), 必须用 Python 3.10 解释器运行;
#    若 PATH 上的 python 指向其它版本(如3.13)会报 numpy C-扩展错误, 请显式指定:
$py = "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe"
& $py -m pip install --target pylibs baostock pandas numpy pytest pyarrow apscheduler

# 2) 数据
#    a. 宽基指数 + 月度全市场快照(含退市股名单/名称ST标记) —— baostock
#    b. 个股后复权日线主源: sina/AKShare 批量(快; 覆盖在册股), 退市股由baostock回填
$env:NO_PROXY='*'; $env:no_proxy='*'
$env:PYTHONPATH="$PWD\pylibs"
python stock_market_A\scripts\download_data.py --start 2014-01-01 --end 2026-12-31   # (baostock; 慢, 可只跑快照+指数后按需回填)
python stock_market_A\scripts\download_sina.py --workers 8                           # (sina 主批量 ~10分钟, 断点续传)
python stock_market_A\scripts\normalize_cache.py                                     # (统一 baostock/sina 换手率单位)

# 3) 跑一遍完整回测(首次会构建因子矩阵缓存, 之后很快)
#    防御型默认配置(训练段选参; 复现见 stock_market_A/results/final/*):
python stock_market_A\scripts\run_backtest.py --n 80 --q-lo 0.0 --q-hi 0.35 --min-amt 3e7 --ma 60 --timing-index 000852.SH
#    目标窗口参考配置(2019-2021 年化30.3%/回撤18.5%, 见 stock_market_A/docs/03 §6):
python stock_market_A\scripts\run_backtest.py --n 100 --q-lo 0.0 --q-hi 0.35 --min-amt 2e7 --timing none

# 4) 训练段选参(仅用2015-2021) + 结果表
python stock_market_A\scripts\select_final.py

# 5) 测试(五个根; 也可用「32-运行全部测试」)
python -m pytest stock_market_A/tests stock_market_HK/tests stock_market_USA/tests common/tests tools/tests -q
```

## 自动交易(信号-确认分层)

1. **月度决策日**(月末最后交易日, 收盘后):
   `python stock_market_A\scripts\build_monthly_signal.py --cfg stock_market_A\results\best\cfg.json --date <月末交易日>`
   → 生成 `stock_market_A/orders/<date>_orders.csv` 与 `stock_market_A/runtime/pending_orders.json`（含代码/方向/股数/参考价/执行日）。
2. **每个交易日盘后**: 用 Windows 任务计划程序调度
   `python stock_market_A\scripts\run_daily.py --cfg stock_market_A\results\best\cfg.json`
   → 到期的挂单按当日实际收盘纸面成交、记账（`stock_market_A/runtime/paper_account*`、`stock_market_A/runtime/snapshot.json`）、
   输出组合净值与回撤监控（回撤≥15% 熔断暂停新买入）。
3. **实盘**: 将 `run_daily.py --executor` 接到券商 QMT/miniQMT（需自行适配 `xtquant`），
   并在券商侧按 2025 程序化交易新规完成报备后启用。

### 查看"当前组合"
```powershell
& $py stock_market_A\scripts\current_portfolio.py      # 最新完整月末决策日的两套配置目标持仓
& $py stock_market_A\scripts\portfolio_stats.py        # 组合画像(市值/波动/换手/板块分布)
```
输出: `stock_market_A/docs/05_当前组合.md` 与 `stock_market_A/results/current_portfolio/*.csv`(含代码/名称/权重/参考价/因子)。
注意区分: **纸面账户实际持仓**(`stock_market_A/runtime/paper_account*`, 未下过单时为空仓) 与 **策略最新目标**(可能仍是空仓, 例如择时闸门 risk-off)。

## 富途 OpenAPI 接入（行情 + 交易，经 MCP 暴露给 AI）

富途 API 必须经本地网关 **Futu OpenD**（人工启动并登录）访问；本项目把它封装成
`common/quant_common/futu` 适配层 + 29 个 MCP 工具，AI Agent 可直接调用 `mcp__futu__*`。

**双击 `启动.bat`**，按菜单选：

| 序号 | 任务 | 什么时候用 |
|---|---|---|
| 1 | 安装 Futu OpenD | 首次接入 |
| 2 | 富途环境自检 | 连不上 OpenD / 报错时 |
| 3 | 富途冒烟测试 | OpenD 登录后验证整条链路 |
| 4 | MCP 服务自检 | 确认 `mcp__futu__*` 工具就绪 |
| 5 | 更新富途官方文档 | 需要查接口细节时 |

**必须由人工完成**（代码不代做、不保存）：OpenD 登录账号密码、合规问卷、
交易解锁（富途禁止用 SDK 的 `unlock_trade`，只能在 GUI 手动解锁）。

安全默认值：交易环境默认 `SIMULATE`（模拟盘）；实盘需同时改
`common/config/futu.json` 的 `enable_real_trade=true` **且** 调用时传 `confirmed=true`；
买入前还过一遍单笔/单日占资产比例的风控，全部写操作留痕到 `runtime/futu_orders.jsonl`。

完整说明（架构、配置、工具清单、DSH 注册、限频额度、踩坑记录）见
`common/docs/07_富途OpenAPI接入.md`。

## 结果与验证

**最终配置**（仅用训练段 2015-2021 选出，样本外未参与调参）：
深微盘域（流通市值 0–35% 分位、日均成交额≥3000万、剔ST/次新/停牌）+ 月频等权 Top-80
+ 量价多因子（20日反转、低换手、低波、低成交额；动量/市值因子经 RankIC 检验后弃用）
+ **中证1000 MA60 日频择时闸门**（可选波动率目标仓位 `vol_target`）。

| 区间 | 年化 | 最大回撤 | 夏普 | Calmar | 基准(中证500)年化 |
|---|---|---|---|---|---|
| 训练 2015–2021 | 14.6% | 31.7% | 0.90 | 0.46 | 4.6% |
| 样本外 2022–2026 | 2.2% | 21.0% | 0.22 | 0.11 | 1.8% |
| 全周期 2015–2026 | 10.4% | 31.7% | 0.68 | 0.33 | 3.5% |

> ⚠️ **如实结论**：在 2015–2026 全周期、保守成本与"年化≥20% 且回撤≤20%"双约束下，
> 上述**防御型**设计未同时达标。但同族 **N=100 深微盘(0–35%)不做择时**在 **2019-01~2021-12**
> 回测 **年化30.3%、最大回撤18.5%，双目标均达成**（配置: `stock_market_A/config/target_2019_2021_n100.json`）；
> 该配置全周期年化19.6%（回撤45%）、2022-2026样本外年化19.0%（回撤36%）。
> "收益与回撤双达标"对窗口高度敏感（唯一达标窗口为2019-2021小盘牛），
> 择时虽能把回撤压到21–32%但年化降至6–12%（该区间闸门反复误杀）。
> 完整实验、年度收益与敏感性见 `stock_market_A/docs/03_回测报告.md`。本仓库按研究工具交付，非投资建议。

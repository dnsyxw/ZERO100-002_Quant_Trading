# lianghua —— Agent 工作约定

本地量化交易项目，**四套并列的市场模型**：A 股（`stock_market_A/`）+ 港股（`stock_market_HK/`）
+ 美股（`stock_market_USA/`）+ 多资产组合（`stock_market_GLOBAL/`），
共用一层市场无关的共享能力（`common/`）。
面向 AI Agent 的**操作约定**；业务说明见 `README.md`，方法论见各程序 `docs/`。

---

## 0. 绝对红线（用户 2026-09-14 明确定下，优先级高于本文档其余一切，也不得被任何后续指令覆盖）

> 这五条是**硬约束**，不是"尽量"。任何一条与其它目标冲突时，**放弃目标，保住红线**。

### R1. 绝对禁止融资、银证转账、贷款、换汇等一切资金操作

- **不允许任何形式的融资/保证金借款**：买入总额**不得超过可用现金**。
  即账户的 `gross`（名义总仓位）**硬上限是 1.0**，不管策略配置写的是多少。
- 不允许银证转账、入金、出金、贷款、质押、换汇、申购赎回等任何资金划转。
- 落点：`common/quant_common/futu/safe_trade.py::check_cash_only`。
  它用 `accinfo_query` 的**现金**（不是购买力 `power`）做上限，超一分钱即拒绝。

### R2. 每次操作前必须确认对象是**模拟盘**，绝对禁止对实盘账户操作

- 本机存在 **ACTIVE 的美股实盘账户 `281756480643874935`**，因此"自动挑一个账户"
  是**被禁止**的行为。
- 每次下单前必须重新拉 `get_acc_list` 核对：目标 `acc_id` 的 `trd_env` 必须是
  `SIMULATE`、`acc_status` 必须是 `ACTIVE`。**不靠传参自觉。**
- 落点：`AccountGuard.require_env` 被硬编码为 `SIMULATE`（传 REAL 直接抛异常），
  `require_acc_id` 必填，`verify_account()` 在**发出任何指令之前**完成核对。

### R3. 只允许对**指定账户**操作；找不到就一律不动

- 经用户指定并经我核验的账户指纹：**持有 1 股 `US.UNH`（联合健康）的美股模拟账户**，
  实测 `acc_id=15188191`（SIMULATE / US / MARGIN / STOCK_AND_OPTION）。
- **每次下单前**都要用 `AccountGuard(anchor_code="US.UNH", anchor_min_qty=1.0)` 复核
  "这个账户里确实有那 1 股 UNH"。指纹不符 → **绝对禁止任何操作**，并停下来问用户。
- 落点：`AccountGuard.anchor_code` + `verify_account()`，由
  `common/tests/futu/test_safe_trade.py::test_账户指纹不匹配时拒绝` 守着。

### R4. 下单只能走 `safe_trade`，不得直连 `gw.call_trade("place_order", ...)`

- 任何新程序/新脚本要下单，都必须调 `quant_common.futu.safe_trade.place_cash_only_batch`。
- `mcp/futu_server.py` 的 `futu_place_order` 只面向**模拟盘**；
  它的 `market` 参数默认是 `CN`，所以下美股单必须**显式传 `market="US"`**，
  否则会错落到 A 股模拟账户（这也是不推荐用 MCP 工具下组合单的原因）。
- **当前 `place_cash_only_batch` 只发 BUY**。所以"需要减仓/清仓"的调仓**不能靠它完成** ——
  必须先在券商 App 人工卖出，再买入缺的部分。给 `safe_trade` 补卖出通道是已知待办
  （见 `stock_market_GLOBAL/docs/13` §2 与 §5），补好之前**不要让模型自动跑调仓**。

### R5. 违反红线的后果由"拒绝执行"承担，不是"记录后继续"

- 五条闸门任一不通过 → **抛异常并中止整批**，不允许"跳过这一笔继续下一笔"。
  （`place_cash_only_batch` 里唯一的例外是**券商侧单笔拒单**，那时整批金额校验已经过了。）

### R6. 红线有回归测试守着，改共享层必须让它们继续通过

`common/tests/futu/test_safe_trade.py`（16 个用例）覆盖：传 REAL 被拒、目标账户是 REAL 被拒、
acc_id 不指定被拒、账户指纹不符被拒、超现金一分钱被拒、恰好用满现金通过、
批量下单逐笔带 `trd_env=SIMULATE` + 正确 acc_id、单笔失败不中断整批。
**这组测试不许删、不许放宽**；改 `safe_trade.py` 后必须整套跑一遍。

---

## 0.1 目录结构（2026-09 重构后，改代码前先记住这张图）

```
lianghua/
├── 启动.bat                 唯一入口（双击 → 中文菜单）
├── launcher/                生成的 .bat 包装（ASCII 铁律见 §1）
├── tools/                   启动器基础设施（跨程序）
│   ├── launcher.py          ★ 任务清单单一真源 TASKS
│   ├── make_launcher.py     按 TASKS 生成 .bat
│   ├── run_tests.py         跑六个测试根
│   └── tests/               启动器回归测试
├── common/                  ── 共享层（市场无关，四个程序都依赖它）
│   ├── quant_common/        metrics / scoring / preprocess / paths
│   │   └── futu/            富途 OpenD 网关（行情+交易+硬风控）
│   ├── config/futu.json     共享配置
│   ├── scripts/             富途自检、文档下载、个股调研
│   └── docs/07_富途OpenAPI接入.md
├── stock_market_A/          ── A股量化程序（自成一体）
│   ├── quant_a/             core / data / factors / strategy / backtest / autotrade
│   ├── scripts/ tests/ config/ docs/ data/ results/ runtime/ orders/
├── stock_market_HK/         ── 港股量化程序（自成一体）
│   ├── quant_hk/            codes / store / source / factors / strategy / engine / runner
│   ├── scripts/ tests/ config/ docs/ data/ results/ runtime/ orders/
├── stock_market_USA/        ── 美股量化程序（自成一体）
│   ├── quant_usa/           codes / store / source / adjust / costs / engine /
│   │                        factors / universe / timing / frames / panels /
│   │                        strategy / runner
│   ├── scripts/ tests/ config/ docs/10–11
│   └── data/us_cache/  results/  runtime/  orders/
├── stock_market_GLOBAL/     ── 多资产组合程序（自成一体，第 4 套）
│   ├── quant_global/        universe / source / store / signals / allocate /
│   │                        engine / backtest / report / config
│   ├── scripts/ tests/ config/ docs/12
│   └── data/gl_cache/  results/
├── mcp/futu_server.py       DSH 的 MCP 服务入口
├── pylibs/                  仓库内依赖（cp310 轮子）
└── runtime/                 共享运行态（富途日志与下单留痕）
```

### 四条不可破坏的约定

1. **四个市场程序互不 import。** 每个只允许 import `quant_common`。
   改美股代码绝不该影响 A 股或港股或组合程序，反之亦然。回归测试守着这条：
   `tools/tests/test_launcher.py::Test程序隔离::test_市场程序互不import`（对四个包参数化）。
2. **共享层不许反向依赖程序包。** `common/quant_common/` 里出现 `quant_a` / `quant_hk` /
   `quant_usa` / `quant_global` 就是隐藏的双向耦合。
3. **`mcp/` 的位置不能动。** DSH 的 MCP 注册写死了
   `~/.dsh/profiles/web/cordis.patch.yml` 里 `<仓库根>\mcp\futu_server.py`，
   挪走要同步改那份外部配置并重启 DSH，否则 `mcp__futu__*` 工具全部失效。
4. **`stock_market_GLOBAL/` 用 Yahoo 而不是新浪, 与美股程序的结论相反 —— 这不是笔误。**
   理由见 `quant_global/__init__.py` 的"数据源取舍"与 `docs/12` §6.1：
   组合程序的 15 只标的都是**当前仍上市**的流动性 ETF(幸存者偏差的前提不存在),
   而新浪 ETF 序列**不含分红**(TLT 股息率约 4%/年)且 **TLT/IEF/SHY/EMB 只有 2016 年后**
   (会丢掉 2008)。改数据源前先跑 `launcher\39-多资产·数据源探针.bat` 复核。

### 路径怎么取（别再写 `parents[N]`）

统一用 `quant_common.paths`：

```python
from quant_common.paths import REPO_ROOT, ashare_path, hk_path, usa_path, global_path, runtime_path
ashare_path("data", "cache")      # stock_market_A/data/cache
hk_path("data", "hk_cache")       # stock_market_HK/data/hk_cache
usa_path("data", "us_cache")      # stock_market_USA/data/us_cache
global_path("data", "gl_cache")   # stock_market_GLOBAL/data/gl_cache
```

脚本入口的引导块（每个脚本自己带一段，保证单独双击也能跑）：

```python
ROOT = Path(__file__).resolve().parents[1]   # 本程序目录, 如 stock_market_GLOBAL/
_PROJ = ROOT.parent                          # 仓库根
for _p in (_PROJ / "pylibs", _PROJ / "common", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
```

---

## 1. 一键启动器（本项目强制约定）

**任何需要用户"打开终端敲命令"的操作，都必须同时提供一个一键启动文件。**
用户不应该被要求记住 `python stock_market_A\scripts\xxx.py --flags`。

- 入口：项目根 `启动.bat`（双击 → 中文菜单）
- 单任务：`launcher\NN-<中文名>.bat`（每个都能单独双击）
- 任务真源：**`tools/launcher.py` 的 `TASKS`**（不要在 `.bat` 里写逻辑）
- 重新生成包装：`python tools\make_launcher.py`
- 规范化行尾/编码：`python tools\make_launcher.py --fix`
- 回归测试：`tools/tests/test_launcher.py`（守着下面三条铁律 + 程序隔离）

新增一个任务 = 在 `TASKS` 里加一行 + 跑一次 `make_launcher.py`。细则见 `launcher/README.md`。

> 任务里的相对路径（如 `--out stock_market_HK/results/x.json`）是**相对仓库根**的，
> 因为 `launcher.py` 以仓库根为 cwd 起子进程。

### Windows 启动器三条铁律（都已实测，别重踩）

1. **`.bat` 里绝对不能出现 `chcp`。** 在批处理文件中间调用 `chcp 65001` 会让 cmd.exe
   丢失对同一文件后续行的解析位置 —— `set /p` 之后的分支全部不执行，**纯 ASCII 文件也一样**。
2. **`.bat` 内容只写 ASCII，中文全部交给 Python 输出。** 一个 `.bat` 无法同时适配
   cp936 与 UTF-8 两种环境；而 Python 可以按 stdout 是不是控制台自动选对编码
   （`common/quant_common/futu/_bootstrap.py::ensure_utf8_stdio()`）。
3. **目录名也必须是 ASCII**（`launcher/`，不是 `启动器/`），否则根 `启动.bat` 的 `call`
   路径里就不得不出现中文，又回到第 2 条。**文件名**可以是中文 —— 那是 NTFS 的 UTF-16
   名称，由资源管理器直接传给 cmd，不经过批处理文件的字节编码。

落盘格式固定为 **UTF-8 无 BOM + CRLF**。

---

## 2. 运行环境（不要猜解释器）

- 依赖装在仓库内 `pylibs/`（cp310 轮子），**必须用 Python 3.10**：
  `%LOCALAPPDATA%\Programs\Python\Python310\python.exe`
  用别的版本会报 numpy C 扩展错误。
- 跑任何脚本都要带 `PYTHONPATH=<项目根>\pylibs`；`pytest` 同理
  （`python -m pytest` 需要 pylibs 已在 path 上）。脚本自己的引导块会补 `common/` 与本程序目录。
- 一键启动器已自动处理这两件事；手工跑时记得自己设。
- 本机 PowerShell 的 .NET TLS 不可用（`Invoke-WebRequest` / `curl.exe` 连 HTTPS 会失败），
  **需要联网抓取时用 Python**（`urllib`/`requests` 正常）。
- **受限沙箱里禁止建进程间管道**（`CreatePipe` → WinError 5）。两个后果：
  1. 想**采集**子进程输出时不要用 `capture_output=True` / `$x = cmd`；改成让子进程
     自己重定向到文件，再读文件。`tools/git_sync.py::_exec_git` 就是"先试管道、
     失败退回临时文件"的现成写法，可直接照抄。
  2. `git` 的**连远端**命令（`ls-remote` / `fetch` / `push` / `pull`）即使能起进程也会失败，
     报 `cannot create standard input pipe for remote-https` —— 这是沙箱限制，不是 git 坏了、
     更不是凭据问题。要联网操作 git 就放宽文件权限，或让用户双击启动器（不受此限制）。

---

## 3. 富途 OpenAPI 接入

- 说明文档：`common/docs/07_富途OpenAPI接入.md`（架构 / 配置 / 工具清单 / DSH 注册 / 限频额度）
- 适配层 `common/quant_common/futu/`；MCP 服务 `mcp/futu_server.py`（29 个工具，DSH 侧名为 `mcp__futu__*`）
- **必须经本地 OpenD 网关**，且 OpenD 的登录 / 交易解锁只能人工在 GUI 完成：
  - 不保存、不读取、不代填任何账号密码；
  - **不实现 `unlock_trade`**（富途官方明令禁止通过 SDK 解锁）；
  - 实盘默认关闭：需要 `common/config/futu.json` 的 `enable_real_trade=true` **且** 调用时
    显式传 `confirmed=true`，再加本地比例风控；写操作留痕到 `runtime/futu_orders.jsonl`。
- 排障顺序：`launcher\02-富途环境自检.bat` → `launcher\03-富途冒烟测试.bat`。
- 需要接口字段/枚举/限频的准确答案时，查 `_futu_raw/Futu-API-Doc-zh-Python.md`
  （官方完整文档，`python common\scripts\fetch_futu_docs.py` 可重新下载），不要凭记忆猜。

---

## 4. 改动与验证

- 改完代码跑：`launcher\41-运行全部测试.bat`，或按范围只跑一边（**用 `tools/run_tests.py`
  分程序跑，不要直接 `python -m pytest`** —— pytest 本身装在 `pylibs/`，直接调 `-m pytest`
  会报 `No module named pytest`；`run_tests.py` 会把 `pylibs`+`common`+四个程序目录经
  `PYTHONPATH` 传给子进程）：

  | 范围 | 命令 | 用例数 |
  |---|---|---|
  | 全部 | `python tools\run_tests.py` | 766 |
  | A 股 | `python tools\run_tests.py stock_market_A/tests` | 52 |
  | 港股 | `python tools\run_tests.py stock_market_HK/tests` | 121 |
  | 美股 | `python tools\run_tests.py stock_market_USA/tests` | 183 |
  | 多资产组合 | `python tools\run_tests.py stock_market_GLOBAL/tests` | 41 |
  | 共享层 | `python tools\run_tests.py common/tests` | 184（含 `futu/test_safe_trade.py` 16 个红线用例） |
  | 启动器 + 同步 | `python tools\run_tests.py tools/tests` | 185（`test_launcher.py` 155 + `test_git_sync.py` 30） |

  必须手工跑 `-m pytest` 时，记得自己带上：
  `$env:PYTHONPATH="$PWD\pylibs;$PWD\common;$PWD\stock_market_A;$PWD\stock_market_HK;$PWD\stock_market_USA;$PWD\stock_market_GLOBAL"`。
- 测试用 `--import-mode=importlib`（见 `pytest.ini`）：多个程序目录下有同名测试文件
  （如 `stock_market_A/tests/test_engine.py`、`stock_market_HK/tests/test_engine.py`、
  `stock_market_USA/tests/test_engine.py`），传统 prepend 模式会按 basename 撞车，
  importlib 模式按相对 rootdir 的路径取名，天然唯一。
- 新增 Python 模块请沿用仓库风格：`from __future__ import annotations`、中文 docstring、
  类型标注、`__all__`。
- `runtime/`、`stock_market_A/runtime/`、`stock_market_HK/runtime/`、`stock_market_USA/runtime/`、
  `*/results/`、`*/data/*cache/`、`_futu_raw/`
  都是 gitignored 的运行态/下载产物，往里写东西不要紧，但**别把其中的内容当源码**。

---

## 5. 美股特有的坑（新增市场时最容易重踩）

1. **新浪美股序列是「原生未复权价」**（三条独立判据见
   `stock_market_USA/docs/10_美股方法论调研.md` §2.3）。因此
   **必须经 `quant_usa.adjust.adjust_for_splits` 还原拆股**，否则序列里会留着
   −85% / −90% / +677% 的假跳变，反转/低波因子会把它当"深度超跌"而重仓买入。
2. **成交额只能用 `close × volume`（`dollar_volume` 列），不能用新浪的 `amount`** ——
   后者只在近期有值（AAPL 自 2017-07 起），且与复权口径不自洽。
3. **股票池必须显式剔除 SPAC**（`frames.is_spac_name` + `universe.exclude_spac`）。
   SPAC 股价恒在 10 美元面值附近、波动率≈0，会**稳定霸占低波/低 MAX 因子的最优端**，
   把因子排序变成噪声。这是美股独有的陷阱。
4. **新浪清单接口有硬上限**：`num` 参数无效（恒 20 条/页），翻完 906 页去重后约
   6,300 条 → 清洗后 3,363 只普通股（NASDAQ/NYSE/AMEX）。**这是清单本身的边界，不是抓取失败。**
5. **新浪会限频（HTTP 456）**：高频请求后**全站**返回 456，连单只日K也跟着 456，持续数分钟。
   下载脚本已内置节流 + `_retry` 对 456 单独长退避 + 连续失败熔断；
   被限频时用 `--workers 2 --pause 0.4` 重跑（断点续传，不会重复下载）。
6. **`hq.sinajs.cn` 的响应 key 是 `hq_str_gb_xxx`**，不是 `gb_xxx` ——
   少剥一层前缀会让解析**静默返回 0 条**（HTTP 200 但空表），表现为
   "shares 全缺 → 换手率/市值因子整列 NaN"。

---

## 6. 「静默失效」防线（四程序通用）

这四个坑本项目都实际踩过，且**都不报错**，只在结果上表现为"一股没买"、"因子全 NaN"
或"净值全 NaN"：

1. **pandas 列赋值会按标签对齐**。`out[name] = s.reindex(dates)` 会去 `dates` 里找
   `s` 自己的标签，结果**全是 NaN**。必须 `.to_numpy()` 赋值
   （见 `quant_usa.factors.per_stock_decision_frame` 的 `put()` helper）。
2. **rolling 结果的 index 是 RangeIndex**。直接 `reindex(DatetimeIndex)` 也会
   **静默返回全 NaN**。要先把日期索引贴回去。
3. **`0 / NaN` 会传染成 NaN 权重。** 组合程序里"尚未上市的资产"其波动率是 NaN,
   若只用 `tilt=0` 而不把它从计算里剔除, 权重会变成 `0/NaN = NaN` 并一路污染净值
   (表现为"回测跑得通, 但净值从第 271 天起全是 NaN")。
   防线: `quant_global.allocate.target_weights` 只在 `live` 掩码上做除法,
   并由 `tests/test_allocate.py::test_尚未上市的资产权重严格为零且不产生NaN` 守着。
4. **路径依赖的风控不能用"实际净值"当状态变量。** 回撤调速器一旦把仓位打到 0,
   实际净值就冻结、回撤永不恢复 → **永久锁死**。必须用"同一套权重但不调速"的
   **影子净值**(且影子同样要付交易成本)当状态。
   防线: `quant_global.engine` 的 `nav_raw`, 由
   `tests/test_engine.py::test_调速器不会永久锁死` 守着。
5. 与之配套的**防线**：`quant_usa.strategy.build_schedule` 在"目标权重全为 0"时
   **必须抛异常**并说清是哪条卡住的（`min_stocks_in` 过大 / 股票池过严 / 因子名拼错），
   绝不允许"回测跑通但一股没买"这种失败模式再出现。

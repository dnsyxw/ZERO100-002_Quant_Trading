# 07 · 富途 OpenAPI 接入

> 目标：把**富途 OpenAPI** 接进本项目，并把行情/交易能力以 **MCP 工具**的形式交给 AI Agent
> （DSH Web GUI 里可直接调用 `mcp__futu__*`）。
>
> 官方文档：<https://openapi.futunn.com/futu-api-doc/intro/intro.html>
> 本文档基于官方文档 v10.10 / Python SDK `futu-api 10.10.7008` 编写。

---

## 1. 架构：富途 API 不是"直连"，而是"经 OpenD"

```
┌──────────────┐   MCP(stdio)   ┌────────────────────┐
│  DSH / AI    │ ─────────────► │ mcp/futu_server.py │
│  Agent       │ ◄───────────── │  (29 个工具)        │
└──────────────┘                └─────────┬──────────┘
                                          │ 进程内调用
                                ┌─────────▼──────────┐
                                │ common/quant_common/futu/     │  适配层
                                │  gateway/quote/    │  连接管理 + 硬风控
                                │  trade/config      │
                                └─────────┬──────────┘
                                          │ futu-api SDK (TCP)
                                ┌─────────▼──────────┐
                                │ Futu OpenD (GUI)   │  ← 必须人工启动 + 登录
                                │ 127.0.0.1:11111    │
                                └─────────┬──────────┘
                                          │
                                    富途服务器
```

**关键认知**：`futu-api` 只是一个本地网关的客户端。**没有 OpenD 就没有任何行情和交易**。
OpenD 的登录、合规问卷、交易解锁都必须在图形界面上人工完成。

---

## 2. ★ 需要你（人类）提供 / 操作的部分

代码已全部就位，以下三步**只能由你本人完成**，做完后本接入即通电：

| # | 事项 | 操作位置 | 代码里是否保存 |
|---|---|---|---|
| 1 | **OpenD 安装包** | `python common/scripts/install_opend.py` 自动下载（Windows 版 10.10.7008） | 不涉及 |
| 2 | **富途账号 + 密码**（牛牛号 / 注册手机号 / 邮箱） | OpenD GUI 登录框 | **不保存、不读取、不代填** |
| 3 | **交易解锁密码** | OpenD GUI 点「解锁交易」 | **不保存**；且富途官方禁止用 SDK 的 `unlock_trade` 解锁，本项目**不提供**该接口 |

可选但影响能力的项：

| 项 | 影响 | 说明 |
|---|---|---|
| 完成合规问卷 + 协议确认 | 首次登录必须做 | 否则 API 不可用 |
| 行情权限 | A 股：境内认证客户免费 LV1；港股：境内免费 LV2；美股：推广期免费 LV3 | 以 OpenD 登录 IP 归属地判定 |
| 账户资产等级 | 订阅额度 / 历史 K 线额度：<1万HKD → 100；≥1万 → 300；≥50万 → 1000；≥500万 → 2000 | 系统自动分配 |
| 是否要实盘交易 | 默认只允许**模拟盘**（SIMULATE） | 要实盘需改 `common/config/futu.json` 的 `enable_real_trade: true` |

> 这三项之外的任何"API Key / Secret"都不存在——富途 OpenAPI **不使用 API Key**，
> 它用的是"OpenD 已登录会话 + 交易解锁状态"。所以"略过需要 key 的部分"在这里等价于
> "把 OpenD 登录/解锁留给人工"。

---

## 3. 快速开始（4 步）

> **直接双击项目根目录的 `启动.bat`**，按序号选即可；等价命令行在下面。
> 每个任务也能在 `launcher\` 下单独双击运行（见 `launcher/README.md`）。

```powershell
# 统一前提：用 Python 3.10 解释器 + 仓库内依赖
$py = "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe"
$env:PYTHONPATH = "$PWD\pylibs"

# 1) 环境自检 —— 一眼看出还差什么              [菜单 2]
& $py common\scripts\check_futu_env.py

# 2) 下载 OpenD 安装包（自动取最新版直链），然后手工安装并登录   [菜单 1]
& $py common\scripts\install_opend.py --launch

# 3) OpenD 启动并登录后，再做一次自检（--deep 会读交易账户）      [菜单 2]
& $py common\scripts\check_futu_env.py --deep

# 3.5) 冒烟测试：一条命令验证 连接→快照→K线→额度→搜索→日历(+交易账户)  [菜单 3]
& $py common\scripts\futu_smoke.py --trade

# 4) 验证 MCP 服务（离线自检 + 打印工具清单）                    [菜单 4]
& $py mcp\futu_server.py --selftest
& $py mcp\futu_server.py --list-tools
```

在 DSH 里使用时，MCP 服务由 `~/.dsh/profiles/web/cordis.patch.yml` 自动拉起（见 §6）。

---

## 4. 文件清单

| 路径 | 作用 |
|---|---|
| `common/quant_common/futu/_bootstrap.py` | **import futu 之前**的进程准备：把 futu 日志目录从 `%appdata%` 挪进 `runtime/`；MCP 模式下把 stdout 让给协议层 |
| `common/quant_common/futu/config.py` | 配置装配（`common/config/futu.json` + `FUTU_*` 环境变量），含风控阈值 |
| `common/quant_common/futu/codes.py` | 代码互转：`600000` ↔ `SH.600000`、`000852.SH` ↔ `SH.000852` |
| `common/quant_common/futu/gateway.py` | OpenD 连接管理：惰性建连、**防阻塞**（见 §7）、健康检查、返回码→异常 |
| `common/quant_common/futu/quote.py` | 行情能力：快照/K线/摆盘/逐笔/分时/资金流/选股/板块/额度… |
| `common/quant_common/futu/trade.py` | 交易能力：账户/资金/持仓/订单/下单/改单/撤单 + 本地硬风控 + 下单留痕 |
| `common/quant_common/futu/errors.py` | 错误类型 |
| `mcp/mcp_stdio.py` | 零依赖 MCP stdio 服务端框架（JSON-RPC 2.0） |
| `mcp/futu_server.py` | 富途工具集装配（29 个工具）+ CLI（`--selftest` / `--list-tools` / `--call`） |
| `common/scripts/install_opend.py` | 下载/解压 OpenD 安装包，给出人工安装登录指引 |
| `common/scripts/check_futu_env.py` | 环境自检 |
| `common/scripts/futu_smoke.py` | 冒烟测试：OpenD 登录后一条命令验证整条链路 |
| `common/scripts/fetch_futu_docs.py` | 下载官方完整 Markdown 文档与官方 Skills 包到 `_futu_raw/`（供离线检索） |
| `tools/launcher.py` | 一键启动器的任务清单 + 中文菜单（**任务唯一真源**） |
| `tools/make_launcher.py` | 按任务清单生成 `launcher\*.bat` 与根 `启动.bat` |
| `启动.bat` / `launcher\` | 一键启动：双击 → 中文菜单；每个任务也能单独双击。见 `launcher/README.md` |
| `common/config/futu.json` | 连接参数 + 风控参数（**不含任何密码**） |
| `runtime/futu_appdata/` | futu SDK 日志（gitignored） |
| `runtime/futu_orders.jsonl` | 全部下单/改单/撤单留痕（gitignored） |
| `runtime/futu_risk_state.json` | 当日累计买入金额（单日风控用） |

---

## 5. 配置说明

`common/config/futu.json`（环境变量可覆盖，见括号）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `host` | `127.0.0.1` | OpenD 监听地址（`FUTU_HOST`） |
| `port` | `11111` | OpenD API 端口（`FUTU_PORT`） |
| `sync_query_timeout` | `30.0` | SDK 同步查询超时（秒）（`FUTU_SYNC_TIMEOUT`） |
| `market` | `CN` | 默认市场：`CN`(→上交所前缀) / `HK` / `US`（`FUTU_MARKET`） |
| `security_firm` | `FUTUSECURITIES` | 券商标识；牛牛国际为 `FUTUINC`，新加坡为 `FUTUSG`（`FUTU_SECURITY_FIRM`） |
| `trd_env` | `SIMULATE` | 默认交易环境；`REAL` 为实盘（`FUTU_TRD_ENV`） |
| `acc_id` | `0` | `0` = 自动取账户列表第一个可用账户（`FUTU_ACC_ID`） |
| `acc_index` | `0` | 自动选择时的下标（`FUTU_ACC_INDEX`） |
| `enable_real_trade` | `false` | **实盘总闸**；`false` 时所有 `trd_env=REAL` 操作被拒（`FUTU_ENABLE_REAL_TRADE`） |
| `max_order_pct` | `0.05` | 单笔买入金额 / 总资产 上限 |
| `max_daily_buy_pct` | `0.30` | 单日累计买入 / 总资产 上限 |
| `log_dir` | `runtime/futu_appdata` | futu 日志根目录（`FUTU_LOG_DIR`） |
| `order_log` | `runtime/futu_orders.jsonl` | 下单留痕（`FUTU_ORDER_LOG`） |
| `watchlist_group` | `全部` | 自选股分组名 |
| `default_kline_max` | `500` | `futu_kline` 默认返回根数 |
| `max_rows` | `2000` | 单次工具调用返回的最大行数（防止撑爆模型上下文） |

---

## 6. 在 DSH 中启用 MCP 服务

MCP 服务由 DSH 的 `dsh-mcp-client` 插件以 **stdio 子进程**方式拉起。
编辑 `~/.dsh/profiles/web/cordis.patch.yml`，加入一条 insert：

```yaml
[ { id: modlens, name: "@liustack/modlens", disabled: true },
  { insert:
      [ { id: futu-openapi
        , name: '@deepseek-ai/dsh-mcp-client'
        , config:
            serverName: futu
            transport: stdio
            command: 'C:\Users\<你>\AppData\Local\Programs\Python\Python310\python.exe'
            args: ['H:\AI_ProjectBase\1_AI_CodingProject\lianghua\mcp\futu_server.py']
            cwd: 'H:\AI_ProjectBase\1_AI_CodingProject\lianghua'
            env:
              PYTHONUTF8: '1'
              PYTHONIOENCODING: 'utf-8'
              PYTHONPATH: 'H:\AI_ProjectBase\1_AI_CodingProject\lianghua\pylibs'
            toolCallTimeoutMs: 60000
            failOnStartupError: false
        }
      ]
  }
]
```

要点：
- `serverName: futu` → 模型看到的工具名是 `mcp__futu__<工具名>`；
- `failOnStartupError: false` → OpenD 没开也不会拖垮 DSH，只是工具调用时返回提示；
- 改完配置需要**重载 profile / 重启 DSH Host** 才会注册工具。

---

## 7. MCP 工具清单（29 个）

### 诊断
| 工具 | 说明 |
|---|---|
| `futu_health` | OpenD 连接/SDK 版本/行情登录/交易登录/账户列表。**出错先调它** |
| `futu_config` | 当前配置 + 仍需人工提供的凭据清单 |

### 行情（读）
| 工具 | 对应 futu 接口 | 备注 |
|---|---|---|
| `futu_market_state` | `get_market_state` | 开闭市状态 |
| `futu_snapshot` | `get_market_snapshot` | **不占订阅额度**，最常用 |
| `futu_kline` | `request_history_kline` | 自动翻页；占历史 K 线额度（7 天） |
| `futu_order_book` | `get_order_book` | 需先订阅 |
| `futu_ticker` | `get_rt_ticker` | 需先订阅 |
| `futu_rt_data` | `get_rt_data` | 分时，需先订阅 |
| `futu_capital_flow` | `get_capital_flow` | 资金流向 |
| `futu_capital_distribution` | `get_capital_distribution` | 大/中/小单分布 |
| `futu_stock_filter` | `get_stock_filter` | 条件选股（SimpleFilter） |
| `futu_plate_list` / `futu_plate_stock` | `get_plate_list` / `get_plate_stock` | 板块与成分股 |
| `futu_stock_basicinfo` | `get_stock_basicinfo` | 名称/上市日/每手股数 |
| `futu_trading_days` | `request_trading_days` | 交易日历 |
| `futu_search_quote` | `get_search_quote` | 关键词搜代码 |
| `futu_subscription` | `query_subscription` | 订阅额度占用 |
| `futu_kline_quota` | `get_history_kl_quota` | 历史 K 线额度余额 |
| `futu_watchlist` | `get_user_security` | 自选股 |

### 交易 / 账户
| 工具 | 对应 futu 接口 | 写操作 |
|---|---|---|
| `futu_accounts` | `get_acc_list` | |
| `futu_funds` | `accinfo_query` | |
| `futu_positions` | `position_list_query` | |
| `futu_orders` | `order_list_query` | |
| `futu_deals` | `deal_list_query` | |
| `futu_history_orders` | `history_order_list_query` | |
| `futu_max_trd_qty` | `acctradinginfo_query` | |
| `futu_place_order` | `place_order` | ✅ |
| `futu_modify_order` | `modify_order` | ✅ |
| `futu_cancel_order` | `cancel_order` | ✅ |

---

## 8. 安全设计（三道闸门 + 留痕）

1. **环境闸门**：`enable_real_trade=false` 时，任何 `trd_env=REAL` 的下单/改单/撤单
   直接抛 `FutuTradeDisabledError`，连 OpenD 都不会碰。
2. **确认闸门**：即使开了实盘总闸，实盘写操作仍必须显式传 `confirmed=true`，
   防止模型"顺手"下单。
3. **风控闸门**：买入前用账户总资产校验
   - 单笔金额 ≤ `max_order_pct`（默认 5%）
   - 当日累计 ≤ `max_daily_buy_pct`（默认 30%）
   - 市价单先用快照价估算金额，取不到参考价则**拒绝下单**（而不是放行）
4. **留痕**：所有写操作追加写入 `runtime/futu_orders.jsonl`。

### 明确不做的事
- ❌ 不提供 `unlock_trade`（富途官方安全规则：必须在 OpenD GUI 手动解锁）；
- ❌ 不保存、不读取、不代填任何账号密码；
- ❌ 不自动启动/登录 OpenD（参数含账号，必须人工）。

---

## 9. 实现中的两个关键坑（已处理）

### 9.1 SDK 会在 import 期写 `%appdata%`
`futu.common.ft_logger` 在 **模块导入时** 就执行
`os.makedirs(os.path.join(os.getenv("appdata"), "com.futunn.FutuOpenD/Log"))`。
在受限环境/无写权限时会直接 `PermissionError`，`import futu` 都失败。
→ `_bootstrap.prepare_process()` 在 import 前把 `appdata` 指向 `runtime/futu_appdata`。

### 9.2 `OpenSecTradeContext` 的构造函数会**永久阻塞**
`OpenContextBase.__init__` 在 `is_async_connect=False`（默认）时是
`while True: try connect; sleep(6)`。OpenD 没启动时构造交易上下文会挂死，
把 MCP 工具调用卡到超时。
→ 行情上下文用 `is_async_connect=True`；交易上下文放到**守护线程**里构造并限时等待
（`TRADE_CONNECT_TIMEOUT = 8s`），超时抛 `FutuNotConnectedError`，后台线程继续尝试，
OpenD 起来后下一次调用自动接上。

另外：futu 的控制台日志 handler 默认挂在 `sys.stdout` 上，会**污染 MCP 的 stdio 协议流**。
→ MCP 进程启动时把 `sys.stdout` 换成 stderr，由协议层独占真 stdout（测试里有一项
"stdout 只有一行报文"专门守这条）。

---

## 10. 与现有量化流程的结合

`quant_common.futu.quote.history_kline_frame()` 直接返回与本项目缓存一致的列结构
（`date / open / high / low / close / volume / turnover`），可用于：

```python
from quant_common.futu import FutuConfig, FutuGateway, quote

gw = FutuGateway(FutuConfig.load())
df = quote.history_kline_frame(gw, "000852.SH", start="2024-01-01", ktype="K_DAY")  # 中证1000
```

典型用途：
- **择时闸门**：本项目现用 `stock_market_A/data/cache` 的 baostock 日线跑 MA60 闸门；富途可作为盘中实时校验源；
- **盘中风控**：`run_daily.py` 目前只用收盘价纸面成交，接上富途后可在盘中检查持仓是否触及止损；
- **实盘执行**：`stock_market_A/scripts/run_daily.py --executor` 的 `qmt/easytrader` 分支可新增 `futu`，
  复用 `quant_common.futu.trade.place_order`（自带三道闸门）。

> ⚠️ 注意：**富途 A 股行情是 LV1 快照，交易能力以港股/美股为主**。
> 本项目主战场（A 股小市值）在富途侧更适合做"数据补充 + 监控"，
> 真正的 A 股下单仍需券商 QMT/miniQMT 通道，并完成程序化交易报备。

---

## 11. 限频与额度（务必节流）

| 项 | 规则 |
|---|---|
| 快照 `get_market_snapshot` | 30 秒内最多 60 次 |
| 下单 | 15 次 / 30 秒（官方 Skills 提示） |
| 订阅额度 | 100 / 300 / 1000 / 2000（按资产/交易量分级），**每标的每类型占 1** |
| 历史 K 线额度 | 同上分级；**7 天内每标的占 1**，同标的换周期不重复计 |
| 期权额度 | 独立计算（20 / 60 / 200 / 400） |

工具层已做节流保护：`futu_kline` 默认只取 500 根并自动翻页，`max_rows=2000` 截断，
避免一次调用就把额度或上下文打满。需要批量拉取时请自行加 sleep 并分批。

---

## 12. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `FutuNotConnectedError: 连不上 OpenD` | OpenD 未启动 / 未登录 / 端口不符。跑 `launcher\02-富途环境自检.bat` |
| 交易上下文 20s 未就绪 | OpenD 起了但**交易未登录**，在 GUI 右侧完成交易登录 |
| `下单失败：交易未解锁` | 到 OpenD GUI 点「解锁交易」输入交易密码（本项目不代做） |
| `futu_kline` 返回 0 行 | 历史 K 线额度耗尽 / 标的停牌 / 区间外。用 `futu_kline_quota` 查余额 |
| 摆盘/逐笔报"未订阅" | 这类接口要求先订阅；先调 `futu_subscribe`，用 `futu_subscription` 看占用 |
| `market is SH, which is not valid` | 富途有三套 market 枚举，用错了。见 §14 |
| import futu 报 PermissionError | `%appdata%` 不可写；本项目已重定向，若仍报错请检查 `runtime/` 写权限 |
| 中文输出乱码 | Windows 控制台是 GBK；脚本已按 isatty 自适应编码，若仍乱码设 `PYTHONIOENCODING=utf-8` |

---

## 13. 官方资料（已下载到 `_futu_raw/`，gitignored）

```powershell
& $py common\scripts\fetch_futu_docs.py     # 重新下载
```

- `_futu_raw/Futu-API-Doc-zh-Python.md` —— 官方完整中文 Python 文档（1.0 MB，30k 行）
- `_futu_raw/skills/` —— 官方 OpenD Skills 包（`futuapi` + `install-futu-opend`，
  含 190+ 参考脚本与 65 个接口速查）

需要更细的字段/枚举时优先查这两处，而不是猜。

---

## 14. 实测记录（2026-09-11 首次接通）

用 `launcher\04-富途全量自检.bat`（即 `common/scripts/futu_live_check.py --trade`）
跑通 **27/27** 项只读接口后记下的真实情况。

### 环境与账户

| 项 | 实测值 |
|---|---|
| OpenD 服务器版本 | `1010` |
| futu-api SDK | `10.10.7008` |
| 行情登录 / 交易登录 | 均为 `true` |
| 交易账户 | `acc_id=15188193`，`SIMULATE` / `CASH` / `trdmarket_auth=['CN']`（**只有模拟盘**） |
| 订阅额度 | 1000，已用 0 |
| 历史 K 线额度 | 999 / 1000 剩余 |
| 自选股 | 191 只（跨港股/美股/A股） |

配置里已把 `acc_id` **固定**成上面这个模拟账户。用 `0`（自动取第一个）虽然也能跑，
但将来 OpenD 里多出实盘账户后有被自动选中的风险 —— 固定更安全。

### 踩到并已修掉的 5 个真问题

1. **富途有三套不同的 market 枚举**，用错接口直接报 `market is SH, which is not valid`：

   | 枚举 | 取值 | 用于 |
   |---|---|---|
   | `Market` | `SH`/`SZ`/`HK`/`US`/`SG`/`JP`/`MY`/`CA`/... | `get_plate_list` / `get_stock_basicinfo` / `get_stock_filter` |
   | `TradeDateMarket` | `CN`/`HK`/`US`/`SG`/`JP`/`MY`/... | **`request_trading_days`** ← A 股要 `CN`，不是 `SH` |
   | `TrdMarket` | `CN`/`HK`/`US`/... | 交易上下文 `filter_trdmarket` |

   → `common/quant_common/futu/codes.py::to_trade_date_market()` + `FutuConfig.trade_date_market`。

2. **`trade_ctx` 死锁**：主线程持锁 `join()` 后台构造线程，而后台线程要拿同一把锁才能
   发布结果 —— 必然等到超时；锁一释放后台立刻完成，表现为"**第一次交易调用必超时，
   第二次就好了**"。修复：`join()` 移到锁外。回归测试 `common/stock_market_A/tests/futu/test_gateway.py`。

3. **`SimpleFilter()` 不接受构造参数**：`SimpleFilter(stock_field=...)` 会 `TypeError`，
   必须 `f = SimpleFilter(); f.stock_field = ...`。→ `quote.py::_build_filter()` 统一处理，
   并校验字段名（传错会列出可用字段）。

4. **`futu_order_book` / `futu_ticker` / `futu_rt_data` 之前根本用不了** —— 它们要求先订阅，
   但工具集里没有订阅入口。已补 `futu_subscribe` / `futu_unsubscribe` / `futu_unsubscribe_all`。

5. **`place_order` 的 `remark` 转 UTF-8 后不能超过 64 字节**（中文一个字 3 字节，
   所以最多约 21 个汉字）。→ 本地提前校验并给出可读错误，不再把服务端的
   `Error variable` 原样抛给模型。

另外：MCP 服务退出时**必须** `gateway.close()`。futu 建连后会留下**非守护线程**，
不关闭则子进程退不出去，DSH 侧会记 `generation did not close within 5000ms`。

### 多市场账户：A 股与港股是**两个不同账户**

这是本项目一个重要的设计修正。富途的资金账户按市场划分，`OpenSecTradeContext` 又带
`filter_trdmarket`，一个上下文只覆盖一个市场。实测账户（全部 SIMULATE）：

| 市场 | acc_id | 类型 | 备注 |
|---|---|---|---|
| `CN`（A 股） | 15188193 | CASH | 现金 100 万 HKD |
| `HK`（港股） | 15188192 | CASH | 现金 99.65 万 HKD |
| `US`（美股） | 15188191 | MARGIN | — |

⚠️ 同一台机器上还存在一个 **ACTIVE 的 REAL 账户** `281756480643874935`
（REAL/MARGIN，auth 覆盖 HK/US/HKCC/SG/HK FUND/USFUND/JP）。

因此：
- 配置改成 `acc_ids: {"CN": ..., "HK": ..., "US": ...}` **按市场固定**，不再用 `acc_id=0` 自动挑；
- `FutuGateway` 按市场缓存交易上下文（`trade_ctx_for(market)`），`call_trade(..., market=)`；
- 下单时若没显式传 `market`，会**从代码前缀推断**（`HK.01810` → 港股账户），不会张冠李戴。

> 用错账户的报错是 `证券账户xxx不支持交易yyy` —— 看到这个就是市场选错了。

### 实盘下单链路实测（模拟盘，2026-09-11 14:44）

按用户要求买了 1 手小米并持有，验证了整条写链路：

```
futu_place_order(code=HK.01810, side=BUY, qty=200, price=26.44, remark="ai-agent sim test 1lot")
  → order_id 9183474, acc_id 15188192(HK/SIMULATE), risk={"total_assets": 996512.211}
  → 状态 FILLED_ALL, 成交 200/200, 均价 26.42
  → 持仓 HK.01810 小米集团-W 200 股, 成本 26.42, 市值 5280 HKD
  → 留痕 runtime/futu_orders.jsonl
```

跑通了「下单 → 风控（单笔占总资产 0.53% < 5%）→ 成交 → 持仓 → 留痕」全链路。

---

## 15. 日常使用

```powershell
# 一键入口
双击 启动.bat            # 菜单
launcher\02-富途环境自检.bat   # 连不上时先跑
launcher\04-富途全量自检.bat   # 逐个接口定位问题
```

在 DSH GUI 里直接用 `mcp__futu__*` 工具（29 + 3 = 32 个）。

**实盘前必须确认的三件事**（默认全是关的）：

1. `common/config/futu.json` 的 `enable_real_trade` 改成 `true`；
2. `acc_ids` 里填的是**实盘**账户（当前填的是模拟账户，这本身就是一道保险）；
3. 在 OpenD GUI 点「解锁交易」并输入交易密码 —— 富途禁止 SDK 解锁，本项目不代做。

### 服务端本来的限制（不是 bug，别误判）

| 现象 | 原因 |
|---|---|
| `unsubscribe` 报 `订阅时间过短，至少需要订阅1分钟` | 富途服务端规则：订阅后 1 分钟内不能取消 |
| `deal_list_query` 报 `模拟交易不支持成交数据` | 模拟盘不提供成交明细；实盘才有 |
| A 股摆盘只有 1 档 | A 股是 LV1 行情权限；港股 LV2 / 美股 LV3 档位更多 |
| 历史 K 线返回 0 行 | 额度用尽 / 标的停牌 / 区间外 —— 用 `futu_kline_quota` 查 |

### 重复自检

日常排障按这个顺序：

1. `launcher\02-富途环境自检.bat` —— 环境 / 进程 / 端口 / 登录
2. `launcher\04-富途全量自检.bat` —— 逐个接口定位问题
3. `launcher\05-MCP服务自检.bat` —— 确认 `mcp__futu__*` 工具本身没问题

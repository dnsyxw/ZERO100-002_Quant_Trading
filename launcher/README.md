# 一键启动器

双击 **项目根目录的 `启动.bat`** 即可打开中文菜单；`launcher\` 下的每个 `.bat` 也可以单独双击。

## 任务一览

> 序号由 `tools/launcher.py` 的 `TASKS` 顺序决定，加任务后重新生成会顺延。
> 下表按**分组**列，具体序号看菜单。

| 分组 | 任务 | 作用 |
|---|---|---|
| 富途 | `01-安装FutuOpenD.bat` | 下载最新版 Futu OpenD 并启动安装向导（装完要**人工**启动并登录） |
| 富途 | `02-富途环境自检.bat` | Python / 依赖 / SDK / 配置 / OpenD 进程与端口 / 登录状态 |
| 富途 | `03-富途冒烟测试.bat` | 快速确认 连接→快照→K线→额度→搜索→日历→账户 是否通 |
| 富途 | `04-富途全量自检.bat` | **把所有只读接口逐个真跑一遍**（含摆盘/逐笔/分时，会自动订阅再释放）—— 定位"哪个接口坏了"用这个 |
| 富途 | `05-MCP服务自检.bat` | 不连 OpenD，只验证 MCP 协议握手 / 工具注册 / stdout 洁净 |
| 富途 | `06-更新富途官方文档.bat` | 重新下载官方接口文档与 Skills 包到 `_futu_raw/` |
| 量化 | `07-下载行情数据.bat` | sina 批量下载个股日线（约 10 分钟，断点续传） |
| 量化 | `08-运行回测.bat` | 防御型默认配置跑一次完整回测 |
| 量化 | `09-生成月度调仓信号.bat` | 需要输入决策日（月末最后交易日） |
| 量化 | `10-每日盘后调度.bat` | 纸面成交 / 择时闸门 / 风控留痕 / 净值快照（可挂任务计划） |
| 量化 | `11-查看当前组合.bat` | 输出 `stock_market_A/docs/05_当前组合.md` 与 `stock_market_A/results/current_portfolio/` |
| **港股** | `12-港股·下载全市场数据.bat` | 元数据+指数+全市场日线（首次 5-8 分钟，断点续传） |
| **港股** | `13-港股·补齐数据缺口.bat` | 首次下载被新浪限频时用；隔一段时间再跑即可继续补 |
| **港股** | `14-港股·因子有效性检验.bat` | 训练段 RankIC / ICIR / 分层，决定用哪些因子 |
| **港股** | `15-港股·训练段选参+样本外验证.bat` | 只在训练段选参数，再跑一次样本外；产出 `stock_market_HK/config/hk_best.json` |
| **港股** | `16-港股·诊断与可达前沿扫描.bat` | 扫池子域×因子×风控，含随机选股/池子等权基准对照 |
| **港股** | `17-港股·运行回测.bat` | 用 `stock_market_HK/config/hk_best.json` 跑；该文件不存在时自动回落到先验配置 |
| **港股** | `18-港股·回测(先验配置).bat` | 不依赖选参结果，用文献先验权重直接跑全周期 |
| **港股** | `19-港股·生成调仓信号.bat` | 按**每手股数**取整生成买卖清单；需要输入决策日 |
| **港股** | `20-港股·每日盘后调度.bat` | 纸面成交 / 风控熔断 / 净值快照，留痕到 `stock_market_HK/runtime/hk_*` |
| 调研 | `21`–`23` | 个股调研的环境自检 / 价格路径 / skill 位置 |
| 质量 | `24-运行全部测试.bat` | 跑 pytest 全量用例（428 个，四个测试根） |

> 港股任务的**推荐顺序**：`12` → （必要时 `13`）→ `14` → `15` → `17`。
> 只想快速看结果：直接 `18`（先验配置，不需要先跑选参）。

## 怎么加一个新启动器

**不要在 `.bat` 里写逻辑。** 任务清单的唯一真源是 `tools/launcher.py` 的 `TASKS`：

```python
Task("my_task", "我的新任务", "stock_market_A/scripts/my_script.py", ("--flag", "x"),
     "一句话说明(会显示在菜单里)", "分组名"),
```

然后重新生成包装文件：

```powershell
python tools\make_launcher.py          # 重新生成 launcher\*.bat 与 启动.bat
python tools\make_launcher.py --list   # 只看看会生成什么
```

---

## 三条实测出来的坑（改启动器前必读）

### 1. `.bat` 里绝对不能出现 `chcp`

实测：在批处理文件**中间**调用 `chcp 65001` 之后，cmd.exe 会丢失对同一文件后续行的
解析位置 —— `set /p` 后面的 `if` 分支**全部不执行**。纯 ASCII 文件同样中招。
所以本项目的 `.bat` 一行 `chcp` 都没有。

### 2. `.bat` 内容只能是 ASCII，中文全部由 Python 输出

一个 `.bat` 无法同时适配 cp936 与 UTF-8：存哪种，另一种环境就乱码。
而 Python 能按 stdout 是不是交互控制台自动选对编码
（见 `common/quant_common/futu/_bootstrap.py::ensure_utf8_stdio()` 与 `tools/launcher.py::_setup_stdio()`）。
因此：**`.bat` 只写英文，菜单/横幅/提示全部是 Python 打印的。**

### 3. 连**目录名**也必须是 ASCII（`launcher/`，不是 `启动器/`）

否则根 `启动.bat` 的 `call "…\启动器\_common.bat"` 里就不得不出现中文，又回到坑 2。
文件名（`01-安装FutuOpenD.bat`）可以是中文 —— 那是 NTFS 的 UTF-16 文件名，
由资源管理器直接传给 cmd，不经过批处理文件的字节编码。

另外 `.bat` 必须是 **UTF-8 无 BOM + CRLF**；`make_launcher.py --fix` 一键规范化。

> 以上三条 + 「两个程序互不 import」都有回归测试守着：`tools/tests/test_launcher.py`。

---

## 环境约定

根目录的 `启动.bat` → `launcher\_common.bat` 会自动：

1. 切到项目根目录；
2. 找 Python 3.10（依次尝试 `%LOCALAPPDATA%\Programs\Python\Python310\python.exe`、
   `py -3.10`、`python`）—— 本项目 `pylibs\` 里是 cp310 轮子，必须 3.10；
3. 设 `PYTHONPATH=<项目根>\pylibs`；
4. 执行任务，结束后 `pause`，方便双击运行时看结果。

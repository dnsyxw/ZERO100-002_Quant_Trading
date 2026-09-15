# 一键启动器

双击 **项目根目录的 `启动.bat`** 即可打开中文菜单；`launcher\` 下的每个 `.bat` 也可以单独双击。

## 任务一览

> **序号会变，别背序号。** 顺序由 `tools/launcher.py` 的 `TASKS` 决定，插入任务会让后面全部顺延。
> 想知道现在到底有哪些任务、各自第几号，看**菜单**（双击 `启动.bat`）或跑
> `python tools\make_launcher.py --list` —— 那才是唯一真源，本表的数字随时可能过期。

| 分组 | 大概的号段 | 干什么 |
|---|---|---|
| 富途 OpenAPI 接入 | `01`–`06` | 装 OpenD / 环境自检 / 冒烟 / 全量自检 / MCP 自检 / 更新官方文档 |
| A股量化模型 | `07`–`11` | 下载行情 → 回测 → 月度调仓信号 → 每日盘后调度 → 查看当前组合 |
| 港股量化模型 | `12`–`22` | 下载 → 补缺口 → 因子 IC → 选参+样本外 → 诊断扫描 → 回撤约束优化 → 回测 → 信号 → 日调度 |
| 美股量化模型 | `23`–`30` | 同上口径（训练段 2005-2014） |
| 个股调研 | `31`–`33` | 环境自检 / 价格路径 / skill 位置 |
| 多资产组合 | `34`–`40` | 下载 ETF → 回测 → 调仓信号 → 下单计划(演练) → 研究扫描 → 数据源探针 |
| 质量 | `41` | 跑 pytest 全量用例（736 个，六个测试根） |
| 同步到 GitHub | `42`–`45` | 推送到 GitHub / 从 GitHub 拉取 / 查看同步状态 / 登录 GitHub |

> 港股、美股的**推荐顺序**：下载 → 因子 IC → 选参+样本外 → 回测。
> 只想快速看结果：直接跑「回测(先验配置)」，不需要先跑选参。
>
> 「同步到 GitHub」四个任务的真源是 `tools/git_sync.py`，用法与红线见根 `README.md`。

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

> 以上三条 + 「四个市场程序互不 import」都有回归测试守着：`tools/tests/test_launcher.py`。
> 与 GitHub 同步的"不许强推、不许自动 stash"底线则由 `tools/tests/test_git_sync.py` 守着。

---

## 环境约定

根目录的 `启动.bat` → `launcher\_common.bat` 会自动：

1. 切到项目根目录；
2. 找 Python 3.10（依次尝试 `%LOCALAPPDATA%\Programs\Python\Python310\python.exe`、
   `py -3.10`、`python`）—— 本项目 `pylibs\` 里是 cp310 轮子，必须 3.10；
3. 设 `PYTHONPATH=<项目根>\pylibs`；
4. 执行任务，结束后 `pause`，方便双击运行时看结果。

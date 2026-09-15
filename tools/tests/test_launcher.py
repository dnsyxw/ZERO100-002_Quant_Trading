"""一键启动器的回归测试。

这些不是"测试代码能不能跑", 而是把两条**实测出来**的 Windows 坑固化下来,
防止以后有人(包括 AI)改坏了:

1. `.bat` 内容必须是**纯 ASCII** —— 中文会让 cp936 / UTF-8 两种环境必有一边乱码;
2. `.bat` 里**绝不能出现 `chcp`** —— 实测在批处理文件中间调 `chcp` 后, cmd.exe 会丢失
   对后续行的解析位置, `set /p` 之后的分支全部不执行(纯 ASCII 文件也一样);
3. 落盘必须是 **UTF-8 无 BOM + CRLF**(LF-only 的批处理同样会让解析错位)。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from launcher import BY_KEY, TASKS  # noqa: E402

LAUNCHER_DIR = ROOT / "launcher"
MENU_BAT = ROOT / "启动.bat"
COMMON_BAT = LAUNCHER_DIR / "_common.bat"


def all_bat_files() -> list[Path]:
    return sorted(LAUNCHER_DIR.glob("*.bat")) + ([MENU_BAT] if MENU_BAT.exists() else [])


#: 任务启动器里引用任务 key 的写法是 `call "%~dp0_common.bat" <key>`。
#: 匹配时必须**要求 key 后面跟行尾**, 否则 `hk_backtest` 会误匹配 `hk_backtest_prior`
#: (前缀包含关系), 造成"文件与注册表一一对应"这条断言假失败。
_TASK_CALL_RE = re.compile(r'_common\.bat"\s+(\S+)\s*$', re.MULTILINE)


def task_key_in(path: Path) -> str | None:
    """从任务启动器里抽出它引用的任务 key(要求整词匹配)。"""
    m = _TASK_CALL_RE.search(path.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def run_python_cli(script: str, *args: str) -> subprocess.CompletedProcess:
    """跑一个 CLI 并把 stdout/stderr 落到文件。

    受限环境下父进程无法为子进程建管道(CreatePipe 会被拒), 因此用文件代替
    `capture_output=True` —— 与 tests/futu/test_stdio_e2e.py 同样的手法。
    """
    tmp = Path(tempfile.mkdtemp(prefix="launcher_cli_"))
    out, err = tmp / "out.txt", tmp / "err.txt"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "pylibs")
    env["PYTHONIOENCODING"] = "utf-8"
    with out.open("w", encoding="utf-8") as fo, err.open("w", encoding="utf-8") as fe:
        code = subprocess.call([sys.executable, str(ROOT / script), *args],
                               cwd=str(ROOT), env=env, stdout=fo, stderr=fe)
    return subprocess.CompletedProcess(
        args=[script, *args], returncode=code,
        stdout=out.read_text(encoding="utf-8", errors="replace"),
        stderr=err.read_text(encoding="utf-8", errors="replace"),
    )


class TestTaskRegistry:
    def test_key_唯一(self):
        keys = [t.key for t in TASKS]
        assert len(keys) == len(set(keys))

    def test_每个任务都有标题与说明(self):
        for task in TASKS:
            assert task.title.strip(), task.key
            assert task.note.strip(), task.key
            assert task.group.strip(), task.key

    def test_脚本都存在(self):
        missing = [t.script for t in TASKS if not (ROOT / t.script).exists()]
        assert missing == [], f"任务指向了不存在的脚本: {missing}"

    def test_需要输入的任务带提示(self):
        for task in TASKS:
            if task.prompt_flag:
                assert task.prompt, f"{task.key} 给了 prompt_flag 但没给 prompt"

    def test_富途任务齐全(self):
        keys = set(BY_KEY)
        assert {"install_opend", "check_env", "smoke", "mcp_selftest"} <= keys

    def test_港股任务齐全(self):
        """港股模型的完整闭环: 下载 -> 因子检验 -> 选参 -> 回测 -> 信号 -> 每日调度。"""
        keys = set(BY_KEY)
        assert {"hk_download", "hk_factor_ic", "hk_optimize", "hk_backtest",
                "hk_signal", "hk_daily"} <= keys

    def test_美股任务齐全(self):
        """美股模型的完整闭环(与港股同一套分层)。"""
        keys = set(BY_KEY)
        assert {"us_download", "us_factor_ic", "us_optimize", "us_backtest",
                "us_signal", "us_daily"} <= keys

    def test_三个市场各有完整任务组(self):
        for prefix, group in (("hk_", "港股量化模型"), ("us_", "美股量化模型")):
            tasks = [t for t in TASKS if t.key.startswith(prefix)]
            assert len(tasks) >= 6, f"{prefix}* 任务只有 {len(tasks)} 个"
            assert {t.group for t in tasks} == {group}


class TestGeneratedLaunchers:
    def test_根菜单存在(self):
        assert MENU_BAT.exists(), "缺少根目录 启动.bat"
        assert COMMON_BAT.exists(), "缺少 启动器/_common.bat"

    def test_任务启动器数量对得上(self):
        per_task = [p for p in LAUNCHER_DIR.glob("[0-9][0-9]-*.bat")]
        assert len(per_task) == len(TASKS), (
            f"任务有 {len(TASKS)} 个, 但 启动器/ 下有 {len(per_task)} 个启动器; "
            "改动 TASKS 后请运行 python tools/make_launcher.py"
        )

    def test_每个任务启动器引用了真实任务(self):
        for path in LAUNCHER_DIR.glob("[0-9][0-9]-*.bat"):
            key = task_key_in(path)
            assert key is not None, f"{path.name} 没有引用任何任务 key"
            assert key in BY_KEY, f"{path.name} 引用了未注册的任务 key: {key}"

    def test_任务启动器与注册表一一对应(self):
        """文件序号顺序必须与 TASKS 顺序一致, 且 key 不重复。"""
        files = sorted(LAUNCHER_DIR.glob("[0-9][0-9]-*.bat"))
        keys_from_files = [task_key_in(p) for p in files]
        assert keys_from_files == [t.key for t in TASKS]

    def test_根菜单不引用中文路径(self):
        """launcher/ 目录名必须保持 ASCII, 否则 启动.bat 里就得塞中文。"""
        text = MENU_BAT.read_text(encoding="utf-8")
        assert "launcher" in text
        assert MENU_BAT.read_bytes().decode("ascii")  # 不抛异常即通过

    @pytest.mark.parametrize("path", all_bat_files(), ids=lambda p: p.name)
    def test_内容纯ASCII(self, path: Path):
        raw = path.read_bytes()
        non_ascii = [b for b in raw if b > 0x7F]
        assert non_ascii == [], (
            f"{path.name} 含非 ASCII 字节 —— 中文必须由 Python 输出, .bat 里只写英文"
        )

    @pytest.mark.parametrize("path", all_bat_files(), ids=lambda p: p.name)
    def test_不含chcp(self, path: Path):
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        # 允许注释里提到 chcp(文档化这个坑), 但不允许真的调用
        effective = [line for line in lowered.splitlines()
                     if not line.strip().startswith("rem")]
        assert "chcp" not in "\n".join(effective), (
            f"{path.name} 调用了 chcp —— 这会让 cmd.exe 丢失后续行的解析位置"
        )

    @pytest.mark.parametrize("path", all_bat_files(), ids=lambda p: p.name)
    def test_无BOM且CRLF(self, path: Path):
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{path.name} 含 UTF-8 BOM"
        assert b"\r\n" in raw, f"{path.name} 不是 CRLF 行尾"
        lone_lf = raw.replace(b"\r\n", b"").count(b"\n")
        assert lone_lf == 0, f"{path.name} 存在裸 LF 行尾"


class TestLauncherCLI:
    def test_list_可运行(self):
        result = run_python_cli("tools/launcher.py", "--list")
        assert result.returncode == 0, result.stderr
        for task in TASKS:
            assert task.key in result.stdout

    def test_未知任务报错(self):
        result = run_python_cli("tools/launcher.py", "--task", "no_such_task")
        assert result.returncode == 1
        assert "未知任务" in (result.stdout + result.stderr)

    def test_make_launcher_list_可运行(self):
        result = run_python_cli("tools/make_launcher.py", "--list")
        assert result.returncode == 0, result.stderr
        assert "_common.bat" in result.stdout


# ============================================================================
# 三套程序相互独立 —— 这是 2026-09 目录重构的核心约定, 必须有回归测试守着。
# 结构: stock_market_A/quant_a(A股) + stock_market_HK/quant_hk(港股)
#      + stock_market_USA/quant_usa(美股) + stock_market_GLOBAL/quant_global(多资产组合)
#      + common/quant_common(共享层)
# ============================================================================

#: 只允许出现在 import 语句里的包名(注释/文档里提到不算)
_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+(quant_\w+)", re.MULTILINE)

#: 各市场/组合程序包 -> 它的目录名。互相之间**一律不许** import。
MARKET_PACKAGES = {"quant_a": "stock_market_A", "quant_hk": "stock_market_HK",
                   "quant_usa": "stock_market_USA", "quant_global": "stock_market_GLOBAL"}


def _imported_packages(pkg_dir: Path) -> set[str]:
    """扫一个包目录里所有 import 到的 quant_* 顶层包名。"""
    found: set[str] = set()
    for py in pkg_dir.rglob("*.py"):
        found.update(_IMPORT_RE.findall(py.read_text(encoding="utf-8")))
    return found


class Test程序隔离:
    """三个市场程序必须能各自独立开发、独立修改, 互不牵连。"""

    def test_目录结构齐全(self):
        for rel in ("stock_market_A/quant_a", "stock_market_HK/quant_hk",
                    "stock_market_USA/quant_usa", "stock_market_GLOBAL/quant_global",
                    "common/quant_common",
                    "stock_market_A/scripts", "stock_market_HK/scripts",
                    "stock_market_USA/scripts", "stock_market_GLOBAL/scripts",
                    "stock_market_A/tests", "stock_market_HK/tests",
                    "stock_market_USA/tests", "stock_market_GLOBAL/tests"):
            assert (ROOT / rel).is_dir(), f"缺少 {rel}"

    @pytest.mark.parametrize("pkg,own_dir", sorted(MARKET_PACKAGES.items()))
    def test_市场程序互不import(self, pkg: str, own_dir: str):
        """改任何一个市场的代码, 都不该影响另外两个(共享层除外)。"""
        forbidden = {p for p in MARKET_PACKAGES if p != pkg}
        used = _imported_packages(ROOT / own_dir)
        overlap = used & forbidden
        assert not overlap, f"{own_dir} 引用了其它市场包: {sorted(overlap)}"

    def test_共享层不反向依赖任何程序(self):
        """共享层必须保持"市场无关", 否则会变成隐藏的双向耦合。"""
        used = _imported_packages(ROOT / "common")
        assert not (used & set(MARKET_PACKAGES)), f"共享层反向引用了程序包: {sorted(used)}"

    def test_三个程序各有独立的脚本与测试(self):
        for prog in MARKET_PACKAGES.values():
            scripts = list((ROOT / prog / "scripts").glob("*.py"))
            tests = list((ROOT / prog / "tests").rglob("test_*.py"))
            assert scripts, f"{prog}/scripts 为空"
            assert tests, f"{prog}/tests 为空"

    def test_每个市场的数据缓存各在自己目录下(self):
        """三个市场的行情绝不能落进同一个目录 —— 代码空间会撞车。"""
        from quant_common.paths import ashare_path, hk_path, usa_path
        a, h, u = ashare_path("data", "cache"), hk_path("data", "hk_cache"), usa_path("data", "us_cache")
        assert len({a, h, u}) == 3
        assert ROOT / "stock_market_A" in a.parents
        assert ROOT / "stock_market_HK" in h.parents
        assert ROOT / "stock_market_USA" in u.parents

    def test_每个市场的config目录独立(self):
        for prog in MARKET_PACKAGES.values():
            assert (ROOT / prog / "config").is_dir(), f"缺少 {prog}/config"

    def test_USA配置文件可被本程序加载(self):
        """`usa_prior.json` 必须能被 `USStrategyConfig.load` 原样读回(不能有未知字段炸掉)。"""
        sys.path.insert(0, str(ROOT / "stock_market_USA"))
        from quant_usa.strategy import USStrategyConfig  # noqa: PLC0415

        cfg = USStrategyConfig.load(ROOT / "stock_market_USA" / "config" / "usa_prior.json")
        assert cfg.as_dict()["market"] == "US"
        assert cfg.n_stocks > 0 and cfg.universe.min_adv > 0


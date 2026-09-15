"""富途 SDK 的进程级准备 —— 必须在任何 `import futu` 之前执行。

两件必须"抢跑"的事:

1. **日志目录**: `futu.common.ft_logger` 在 **import 期** 就会执行
   `os.makedirs(os.path.join(os.getenv("appdata"), "com.futunn.FutuOpenD/Log"))`
   并打开一个 TimedRotatingFileHandler。若 `%appdata%` 不可写(受限环境/无权限),
   `import futu` 直接抛 PermissionError。把 `appdata` 指到本项目 `runtime/` 下,
   既避开权限问题, 也符合本项目"不污染系统环境"的约定。

2. **stdout 隔离**: 该 logger 的 console handler 挂在 `sys.stdout` 上。
   MCP 的 stdio 传输要求 stdout **只有** JSON-RPC 报文, 因此运行 MCP 服务时必须
   先把 `sys.stdout` 换成 `sys.stderr`, 由协议层独占真正的 stdout。

另外负责把仓库内的 `pylibs/` 放到 `sys.path` 上(本项目依赖装在仓库内)。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from ..paths import REPO_ROOT, runtime_path

__all__ = [
    "REPO_ROOT",
    "DEFAULT_APP_DATA_DIR",
    "ensure_pylibs_on_path",
    "ensure_utf8_stdio",
    "prepare_process",
    "sanitize_futu_logging",
]

DEFAULT_APP_DATA_DIR = runtime_path("futu_appdata")

_prepared = False


def ensure_utf8_stdio() -> None:
    """让中文在"控制台"和"管道"两种环境下都正确, 且永不因编码崩溃。

    规则(与 `scripts/launcher.py` 一致):

    - **交互式控制台(isatty)**: 保留系统编码 —— 中文 Windows 是 cp936, 直接输出中文
      就是对的。因此也**不需要**在 `.bat` 里调用 `chcp`: 实测在批处理文件中间调 chcp
      会让 cmd.exe 丢失后续行的解析位置(纯 ASCII 文件也一样)。
    - **管道 / 重定向(非 tty)**: 统一成 UTF-8, 保证日志文件、MCP 的 JSON-RPC、
      以及被其它程序捕获的输出都正确。

    两边都设 `errors="replace"`, 保证任何情况下都不会因为编码异常把程序打挂。
    """
    import sys as _sys

    for stream in (_sys.stdout, _sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")  # type: ignore[union-attr]
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # pragma: no cover - 已重定向/不支持的流
            pass


def ensure_pylibs_on_path() -> None:
    """把仓库内 `pylibs/` 加入 sys.path (本项目依赖的安装位置)。"""
    libs = REPO_ROOT / "pylibs"
    if libs.is_dir():
        entry = str(libs)
        if entry not in sys.path:
            sys.path.insert(0, entry)


def prepare_process(*, app_data_dir: Path | str | None = None, isolate_stdout: bool = False) -> Path:
    """在 `import futu` 之前调用: 重定向 futu 日志目录 + 可选地隔离 stdout。

    Args:
        app_data_dir: futu 日志根目录 (即 futu 眼中的 `%appdata%`); 默认 `runtime/futu_appdata`。
        isolate_stdout: 是否把 `sys.stdout` 换成 `sys.stderr` (MCP stdio 服务必须为 True)。

    Returns:
        实际使用的 futu 日志根目录。
    """
    global _prepared
    root = Path(app_data_dir) if app_data_dir else DEFAULT_APP_DATA_DIR
    # futu 会在其下再拼 "com.futunn.FutuOpenD/Log", 这里一并建好, 避免它自己创建失败。
    (root / "com.futunn.FutuOpenD" / "Log").mkdir(parents=True, exist_ok=True)

    if isolate_stdout:
        _isolate_stdout()

    ensure_pylibs_on_path()
    # 必须在 `import futu` 之前生效: FTLog 在 import 期就会读 appdata 建日志目录。
    os.environ["appdata"] = str(root)
    _prepared = True
    return root


def _isolate_stdout() -> None:
    """把 `sys.stdout` 换成 stderr, 真正的 stdout 交给协议层独占。"""
    real_stdout = sys.stdout
    if getattr(sys, "_futu_real_stdout", None) is None:
        sys._futu_real_stdout = real_stdout  # type: ignore[attr-defined]
    if sys.stdout is not sys.stderr:
        sys.stdout = sys.stderr


def real_stdout():
    """返回被隔离前的真实 stdout (协议流); 未隔离时返回当前 stdout。"""
    return getattr(sys, "_futu_real_stdout", None) or sys.stdout


def sanitize_futu_logging() -> None:
    """`import futu` **之后** 调用: 把 futu 的控制台日志压到 stderr 并降噪。

    futu 的 FTLog 是模块级单例, import 时就已建好 handler; 这里做两件兜底:
    - 把 console handler 的输出流从 stdout 改到 stderr (防止污染 MCP 协议流);
    - 把控制台级别提到 WARNING, 避免刷屏。
    """
    try:
        from futu.common import ft_logger  # type: ignore
    except Exception:  # pragma: no cover - SDK 缺席时静默跳过
        return
    try:
        inst = getattr(ft_logger.FTLog, "instance", None)
        if inst is None:
            ft_logger.logger  # noqa: B018 - 触发单例实例化
            inst = getattr(ft_logger.FTLog, "instance", None)
        if inst is None:
            return
        handler = getattr(inst, "consoleHandler", None)
        if handler is not None:
            handler.stream = sys.stderr
        console_logger = getattr(inst, "console_logger", None)
        if console_logger is not None:
            console_logger.setLevel("WARNING")
    except Exception:  # pragma: no cover - 防御: 任何异常都不应阻断业务
        pass

"""富途接入环境自检 —— 一条命令看清"还差什么"。

用法::

    python scripts/check_futu_env.py                 # 只检查, 不改动环境
    python scripts/check_futu_env.py --deep          # 额外查询交易账户(需 OpenD 已交易登录)
    python scripts/check_futu_env.py --install       # 缺失时用 pip 安装 futu-api(需联网)

检查项: Python 版本 -> 仓库内依赖目录 -> futu-api SDK -> 配置 -> OpenD 进程/端口 ->
        OpenD 登录状态 -> 行情/交易权限提示。
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]        # common/
_PROJ = ROOT.parent                               # 仓库根
for _p in (_PROJ / "pylibs", ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

OK = "[ OK ]"
WARN = "[WARN]"
FAIL = "[FAIL]"

results: list[tuple[str, str, str]] = []


def record(level: str, item: str, detail: str = "") -> None:
    results.append((level, item, detail))
    print(f"{level} {item}" + (f" — {detail}" if detail else ""))


def check_python() -> None:
    major, minor = sys.version_info[:2]
    if (major, minor) == (3, 10):
        record(OK, f"Python {major}.{minor}.{sys.version_info[2]}", "与 pylibs 内的 cp310 轮子匹配")
    elif (major, minor) >= (3, 9):
        record(WARN, f"Python {major}.{minor}", "本项目 pylibs 内是 cp310 轮子; 换解释器可能报 C 扩展错误")
    else:
        record(FAIL, f"Python {major}.{minor}", "需要 Python >= 3.9")


def check_pylibs() -> None:
    libs = _PROJ / "pylibs"
    if libs.is_dir():
        record(OK, "依赖目录 pylibs/", str(libs))
    else:
        record(WARN, "依赖目录 pylibs/ 不存在", "请按 README 安装依赖(pandas/numpy/pytest)")


def check_sdk() -> bool:
    from quant_common.futu._bootstrap import prepare_process

    # futu 在 import 期就会在 %appdata% 下建日志目录 —— 先把它导向仓库 runtime/,
    # 否则受限环境下 import 直接抛 PermissionError。
    prepare_process()
    try:
        import futu  # type: ignore
    except Exception as exc:
        record(FAIL, "futu-api SDK 未就绪", f"{type(exc).__name__}: {exc}")
        return False
    version = None
    try:
        version = (Path(futu.__file__).parent / "VERSION.txt").read_text(encoding="utf-8").strip()
    except Exception:
        version = getattr(futu, "__version__", "未知")
    record(OK, f"futu-api SDK {version}", str(Path(futu.__file__).parent))
    try:
        import Crypto  # noqa: F401
        record(OK, "依赖 PyCryptodome 可用")
    except Exception as exc:
        record(WARN, "PyCryptodome 缺失", f"{exc}; 加密连接时会失败")
    try:
        import google.protobuf as pb  # type: ignore
        record(OK, f"protobuf {pb.__version__}")
    except Exception as exc:
        record(WARN, "protobuf 缺失", str(exc))
    return True


def check_config(path: str | None):
    from quant_common.futu import FutuConfig
    from quant_common.futu.errors import FutuConfigError

    try:
        cfg = FutuConfig.load(path)
    except FutuConfigError as exc:
        record(FAIL, "配置加载失败", str(exc))
        return None
    record(OK, "配置加载成功", f"{cfg.host}:{cfg.port} market={cfg.market} trd_env={cfg.trd_env} "
                                f"enable_real_trade={cfg.enable_real_trade}")
    return cfg


def check_port(cfg) -> bool:
    try:
        with socket.create_connection((cfg.host, cfg.port), timeout=1.5):
            record(OK, f"OpenD 端口可连接 {cfg.host}:{cfg.port}")
            return True
    except OSError as exc:
        record(FAIL, f"OpenD 端口不可连接 {cfg.host}:{cfg.port}", str(exc))
        return False


def check_opend_process() -> None:
    if sys.platform != "win32":
        return
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Futu_OpenD*"],
                             capture_output=True, text=True, timeout=15)
        text = (out.stdout or "") + (out.stderr or "")
        if "Futu_OpenD" in text:
            record(OK, "检测到 OpenD 进程", "Futu_OpenD*")
        else:
            record(WARN, "未检测到 OpenD 进程", "请启动 Futu OpenD 桌面端(GUI 版)并登录")
    except Exception as exc:  # pragma: no cover
        record(WARN, "OpenD 进程检测失败", str(exc))


def check_live(cfg, deep: bool) -> None:
    from quant_common.futu import FutuGateway

    gw = FutuGateway(cfg)
    state = gw.health(deep=deep)
    if state.get("connected"):
        opend = state.get("opend", {})
        record(OK, "OpenD 登录状态", json.dumps(opend, ensure_ascii=False)[:400])
        if not opend.get("qot_logined"):
            record(WARN, "行情未登录", "请在 OpenD GUI 完成行情登录")
        if not opend.get("trd_logined"):
            record(WARN, "交易未登录", "行情可用; 需要交易时请在 OpenD GUI 完成交易登录")
        if state.get("trade_error"):
            record(WARN, "交易账户查询失败", str(state["trade_error"])[:200])
        elif state.get("acc_id"):
            record(OK, "交易账户", f"acc_id={state['acc_id']} {state.get('account')}")
    else:
        record(FAIL, "OpenD 未连接", str(state.get("hint") or state.get("error") or "")[:300])
    gw.close()


def do_install() -> None:
    """用 pip 安装 futu-api(用户机器上的常规做法; 本项目把它们装进 pylibs/)。"""
    target = _PROJ / "pylibs"
    cmd = [sys.executable, "-m", "pip", "install", "--target", str(target), "futu-api"]
    record(WARN, "开始安装 futu-api", " ".join(cmd))
    try:
        subprocess.run(cmd, check=False)
    except Exception as exc:  # pragma: no cover
        record(FAIL, "安装失败", str(exc))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None, help="配置文件路径(默认 config/futu.json)")
    ap.add_argument("--deep", action="store_true", help="额外查询交易账户列表")
    ap.add_argument("--install", action="store_true", help="缺失 futu-api 时用 pip 安装")
    args = ap.parse_args()

    from quant_common.futu._bootstrap import ensure_utf8_stdio

    ensure_utf8_stdio()
    print("=" * 72)
    print("富途 OpenAPI 接入环境自检")
    print("=" * 72)

    check_python()
    check_pylibs()
    has_sdk = check_sdk()
    if not has_sdk and args.install:
        do_install()
        has_sdk = check_sdk()

    cfg = check_config(args.config)
    check_opend_process()
    port_ok = check_port(cfg) if cfg else False
    if cfg and port_ok and has_sdk:
        try:
            check_live(cfg, args.deep)
        except Exception as exc:  # pragma: no cover
            record(FAIL, "实时检查异常", f"{type(exc).__name__}: {exc}")

    print("-" * 72)
    failed = [r for r in results if r[0] == FAIL]
    warned = [r for r in results if r[0] == WARN]
    print(f"汇总: OK {len([r for r in results if r[0] == OK])} / WARN {len(warned)} / FAIL {len(failed)}")
    if failed:
        print("\n待办:")
        for _, item, detail in failed:
            print(f"  - {item}: {detail}")
    if not port_ok:
        print("\n下一步: 安装并登录 OpenD")
        print("  python scripts/install_opend.py            # 下载安装包并提示安装")
        print("  启动 Futu OpenD -> 输入牛牛号/密码登录 -> 完成合规问卷")
        print("  交易解锁请在 OpenD GUI 手动点击「解锁交易」(富途禁止 SDK 解锁)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""把本地改动同步到 GitHub, 或把 GitHub 上的更新拉回本地。

一键启动: 项目根 `启动.bat` → 「同步·推送到 GitHub」/「同步·从 GitHub 拉取」,
或直接双击 `launcher\\42-同步·推送到GitHub.bat`。

设计要点(都是本项目实际会踩的):
1. **先看再动**: 任何写操作(commit/push/pull)之前, 先把当前分支、领先/落后提交数、
   将要提交/覆盖的文件清单完整打印出来, 让用户确认。默认还要再输一次 y。
2. **不做危险动作**: 不自动 `git stash`、不自动 rebase、不自动 `--force`。
   远端有本地没有的提交 → 停下来提示先拉取, 绝不强推覆盖。
3. **不碰运行态**: `runtime/`、各市场行情缓存、`pylibs/` 早已写进 `.gitignore`,
   这里不用再操心, 但每次都会打印被忽略的文件数让用户心里有数。
4. **采集 git 输出不靠管道**: 本机 DSH 沙箱禁止创建进程间管道
   (`CreatePipe` → WinError 5), 所以这里**先试管道, 失败就退回到临时文件**。
   代价只是稍微慢一点, 换来的是脚本在受限环境里也能正常跑、能报出真实原因
   (而不是抛一个 PermissionError 让人以为 git 装坏了)。
   注意: 连远端的命令(ls-remote/fetch/push)即使能起进程, 也会因为 git 自己需要
   给 `remote-https` 建管道而失败, 报 "cannot create standard input pipe";
   那是沙箱权限问题, 用户自己双击 .bat 不受影响。

用法::

    python tools/git_sync.py push             # 提交并推送(会先确认)
    python tools/git_sync.py push --yes       # 不确认(给启动器/自动化用)
    python tools/git_sync.py push -m "说明"   # 指定提交信息
    python tools/git_sync.py pull --yes       # 拉取远端更新(快进)
    python tools/git_sync.py status           # 只看状态, 不动任何东西
    python tools/git_sync.py login            # 保存 GitHub 凭据(触发凭据管理器)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRANCH = "main"
REPO_URL = "https://github.com/dnsyxw/ZERO100-002_Quant_Trading.git"
DEFAULT_MESSAGE = "同步: 本地迭代更新"


def _setup_stdio() -> None:
    """中文在两种环境下都正确, 且永不因编码崩溃(与 tools/launcher.py 同一套)。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _exec_git(args: list[str]) -> tuple[int, str]:
    """跑一条 git 命令, 返回 (退出码, stdout+stderr)。

    优先用管道(capture_output); 沙箱禁止建管道时退回临时文件重定向。
    两条路径都不需要调用方关心 —— 这也是本文件唯一与沙箱有关的地方。

    `-c core.quotepath=false`: 否则中文文件名会被 git 转义成
    `"launcher/42-\\345\\220\\214..."` 这种八进制串, 打印出来没法看。
    """
    cmd = ["git", "-c", "core.quotepath=false", *args]
    try:
        proc = subprocess.run(
            cmd, cwd=str(ROOT),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except OSError:
        pass  # 落到下面的文件重定向分支

    out_fd, out_path = tempfile.mkstemp(prefix="git_sync_", suffix=".out")
    err_fd, err_path = tempfile.mkstemp(prefix="git_sync_", suffix=".err")
    os.close(out_fd)
    os.close(err_fd)
    try:
        with open(out_path, "w", encoding="utf-8") as fo, open(err_path, "w", encoding="utf-8") as fe:
            code = subprocess.call(cmd, cwd=str(ROOT), stdout=fo, stderr=fe)
        return code, _read(out_path) + _read(err_path)
    finally:
        for path in (out_path, err_path):
            try:
                os.unlink(path)
            except OSError:
                pass


def _run(args: list[str], *, quiet: bool = True) -> tuple[int, str]:
    """跑 git 并把输出留一份; quiet=False 时实时打印。"""
    code, out = _exec_git(args)
    out = out.strip()
    if not quiet and out:
        print(out)
    return code, out


def _line(char: str = "=") -> None:
    print(char * 76)


def _title(text: str, note: str = "") -> None:
    print()
    _line()
    print(f"  {text}")
    if note:
        print(f"  {note}")
    _line()
    print(f"  仓库目录: {ROOT}")
    print()


def _need_git() -> bool:
    try:
        code, out = _exec_git(["--version"])
    except OSError as exc:
        print(f"[失败] 找不到可用的 git: {exc}")
        print("       请先安装 Git for Windows: https://git-scm.com/download/win")
        return False
    if code != 0:
        print(f"[失败] git 无法运行: {out.strip()}")
        return False
    print(f"[环境] {out.strip()}")
    return True


def _need_repo() -> bool:
    code, _ = _run(["rev-parse", "--is-inside-work-tree"])
    if code != 0:
        print(f"[失败] {ROOT} 不是一个 git 仓库。")
        print("       修复: 在本目录执行  git init -b main  然后重新运行本脚本。")
        return False
    return True


def _ensure_origin() -> bool:
    """确保 origin 指向本项目仓库; 缺失或指错都修正回来。"""
    code, url = _run(["remote", "get-url", "origin"])
    if code != 0:
        print(f"[设置] 添加远端 origin -> {REPO_URL}")
        code, out = _run(["remote", "add", "origin", REPO_URL])
        if code != 0:
            print(f"[失败] 添加 origin 失败: {out}")
            return False
        return True
    if url != REPO_URL:
        print(f"[设置] 修正 origin: {url} -> {REPO_URL}")
        code, out = _run(["remote", "set-url", "origin", REPO_URL])
        if code != 0:
            print(f"[失败] 修正 origin 失败: {out}")
            return False
    else:
        print(f"[环境] origin = {url}")
    return True


def _describe() -> dict[str, object]:
    """收集状态: 分支 / HEAD / 领先落后 / 变更 / 被忽略。"""
    info: dict[str, object] = {}
    _, branch = _run(["rev-parse", "--abbrev-ref", "HEAD"])
    info["branch"] = branch
    _, info["head"] = _run(["log", "--oneline", "-1"])
    _, porcelain = _run(["status", "--porcelain"])
    info["changed"] = [x for x in porcelain.splitlines() if x.strip()]
    _, ignored = _run(["status", "--porcelain", "--ignored"])
    info["ignored_count"] = len([x for x in ignored.splitlines() if x.startswith("!!")])
    ahead_code, ahead = _run(["rev-list", "--count", f"origin/{BRANCH}..{BRANCH}"])
    behind_code, behind = _run(["rev-list", "--count", f"{BRANCH}..origin/{BRANCH}"])
    info["ahead"] = int(ahead) if ahead_code == 0 and ahead.isdigit() else None
    info["behind"] = int(behind) if behind_code == 0 and behind.isdigit() else None
    return info


def _print_state(info: dict[str, object]) -> None:
    print(f"[状态] 当前分支 : {info['branch']}   (目标分支: {BRANCH})")
    print(f"[状态] 最新提交 : {info['head']}")
    ahead, behind = info["ahead"], info["behind"]
    if ahead is None or behind is None:
        print("[状态] 与远端比较: 拿不到远端状态(可能是网络/凭据/沙箱权限问题)")
    else:
        print(f"[状态] 相对远端 : 领先 {ahead} 个提交, 落后 {behind} 个提交")
    changed = info["changed"]
    print(f"[状态] 变更文件 : {len(changed)} 个     (被忽略的运行态/缓存: {info['ignored_count']} 个)")
    if changed:
        show = changed[:40]
        for text in show:
            print("        " + text)
        if len(changed) > len(show):
            print(f"        ... 还有 {len(changed) - len(show)} 个, 完整清单见 git status")


def _explain_remote_failure(out: str) -> None:
    """把连不上远端的原始报错翻译成人话。"""
    if "pipe" in out:
        print("        原因: DSH 沙箱禁止进程间管道, git 无法启动 remote-https 助手。")
        print("              这不是 git 坏了 —— 直接双击 launcher 里的启动器就不受此限制。")
    elif any(k in out for k in ("Authentication", "could not read", "401", "403", "Permission denied (publickey)")):
        print("        原因: GitHub 凭据缺失或失效。")
        print("        处理: 双击 launcher\\45-同步·登录GitHub.bat 完成一次登录。")
    elif any(k in out for k in ("Could not resolve", "unable to access", "timed out", "Failed to connect")):
        print("        原因: 网络不通(本项目所在机器可能需要代理)。")
    elif "not found" in out.lower() or "404" in out:
        print("        原因: 仓库地址或权限不对。私有库需要账号有访问权。")


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        print(f"[确认] {prompt} -> 已自动确认")
        return True
    try:
        answer = input(f"{prompt} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


def _fetch() -> bool:
    """连远端刷新状态; 失败时说清楚原因。"""
    code, out = _run(["fetch", "--prune", "--no-tags", "origin", BRANCH])
    if code != 0:
        print("[警告] 连不上远端, 无法比较领先/落后:")
        print("        " + out.replace("\n", "\n        "))
        _explain_remote_failure(out)
        return False
    return True


def cmd_status() -> int:
    _title("与 GitHub 的同步状态", "只读检查, 不会改动任何文件")
    if not _need_git() or not _need_repo() or not _ensure_origin():
        return 1
    _fetch()
    _print_state(_describe())
    return 0


def cmd_push(message: str, assume_yes: bool) -> int:
    _title("把本地改动同步到 GitHub", f"目标: {REPO_URL}  分支: {BRANCH}")
    if not _need_git() or not _need_repo() or not _ensure_origin():
        return 1

    if not _fetch():
        print()
        print("[中止] 连不上远端就不推 —— 避免在不知情的情况下覆盖别人的提交。")
        return 1

    info = _describe()
    if info["branch"] != BRANCH:
        print(f"[失败] 当前在 {info['branch']} 分支, 本脚本只同步 {BRANCH}。")
        print(f"       切换: git switch {BRANCH}")
        return 1
    _print_state(info)

    behind = info["behind"]
    if isinstance(behind, int) and behind > 0:
        print()
        print(f"[中止] 远端有 {behind} 个本地没有的提交。")
        print("       请先双击「同步·从 GitHub 拉取」把远端更新拉下来, 再推送。")
        print("       (本脚本不做 rebase, 也绝不强推覆盖远端。)")
        return 1

    changed = info["changed"]
    if not changed:
        ahead = info["ahead"]
        if isinstance(ahead, int) and ahead > 0:
            print()
            print(f"[提示] 没有新改动, 但本地领先远端 {ahead} 个提交, 需要推送。")
            if not _confirm("把已有的本地提交推送到 GitHub?", assume_yes):
                print("[取消] 未推送。")
                return 1
            code, _ = _run(["push", "origin", f"{BRANCH}:{BRANCH}"], quiet=False)
            if code != 0:
                print("[失败] 推送失败, 把上面的报错发给 AI。")
                return 1
            print("[完成] 已推送到 GitHub。")
            return 0
        print()
        print("[完成] 本地与远端一致, 没有需要同步的内容。")
        return 0

    print()
    if not _confirm(f"把上面 {len(changed)} 个变更提交并推送到 GitHub?", assume_yes):
        print("[取消] 未做任何改动。")
        return 1

    code, out = _run(["add", "-A"])
    if code != 0:
        print(f"[失败] git add 失败: {out}")
        return 1
    code, staged = _run(["diff", "--cached", "--name-only"])
    staged_files = [x for x in staged.splitlines() if x.strip()]
    if not staged_files:
        print("[提示] 暂存区为空, 无需提交。")
        return 0
    print(f"[执行] 已暂存 {len(staged_files)} 个文件, 开始提交 ...")

    code, out = _run(["commit", "-m", message], quiet=False)
    if code != 0:
        print("[失败] 提交失败, 把上面的报错发给 AI。")
        return 1

    code, out = _run(["push", "origin", f"{BRANCH}:{BRANCH}"], quiet=False)
    if code != 0:
        print("[失败] 提交已在本地生成, 但推送失败。")
        print("       本地提交没有丢: 修好网络/凭据后重跑本任务即可继续推送。")
        return 1

    _, head = _run(["log", "--oneline", "-1"])
    print()
    print(f"[完成] 已推送到 GitHub: {head}")
    print(f"        在线查看: {REPO_URL}")
    return 0


def cmd_pull(assume_yes: bool) -> int:
    _title("把 GitHub 上的更新拉回本地", f"来源: {REPO_URL}  分支: {BRANCH}")
    if not _need_git() or not _need_repo() or not _ensure_origin():
        return 1
    if not _fetch():
        print()
        print("[中止] 连不上远端, 无法拉取。")
        return 1

    info = _describe()
    _print_state(info)
    if info["branch"] != BRANCH:
        print(f"[失败] 当前在 {info['branch']} 分支, 本脚本只同步 {BRANCH}。")
        return 1

    behind = info["behind"]
    if isinstance(behind, int) and behind == 0:
        print()
        print("[完成] 已经是最新, 无需拉取。")
        return 0

    if info["changed"]:
        print()
        print(f"[中止] 本地有 {len(info['changed'])} 个未提交的改动, 拉取可能冲突。")
        print("       请先双击「同步·推送到 GitHub」把本地改动提交并推送, 再拉取。")
        print("       (本脚本不自动 stash, 免得把你的改动藏起来找不回来。)")
        return 1

    print()
    if not _confirm(f"从 GitHub 拉取 {behind} 个提交(快进合并)?", assume_yes):
        print("[取消] 未拉取。")
        return 1

    code, _ = _run(["pull", "--ff-only", "origin", BRANCH], quiet=False)
    if code != 0:
        print("[失败] 拉取失败。若提示 diverged, 说明本地也有远端没有的提交,")
        print("       先推送本地改动再拉取; 仍不行就把上面的报错发给 AI。")
        return 1
    _, head = _run(["log", "--oneline", "-1"])
    print()
    print(f"[完成] 已更新到 {head}")
    print("        提示: 若这次更新改了代码, 建议再双击「运行全部测试」确认一遍。")
    return 0


def cmd_login() -> int:
    _title("登录 GitHub(保存凭据)", "只有第一次或凭据过期时才需要")
    if not _need_git() or not _need_repo() or not _ensure_origin():
        return 1
    print("[说明] 接下来会触发 Git 的凭据管理器窗口, 请按提示用浏览器登录 GitHub 账号。")
    print("       凭据保存在 Windows 凭据管理器里, 本项目不保存、不读取你的密码。")
    print()
    code, out = _run(["fetch", "--prune", "origin", BRANCH], quiet=False)
    if code == 0:
        print("[完成] 凭据可用, 并且已连上远端。")
        return 0
    print("[未成功] 请对照下面的原因处理:")
    _explain_remote_failure(out)
    return 1


def main(argv: list[str] | None = None) -> int:
    _setup_stdio()
    ap = argparse.ArgumentParser(description="lianghua 与 GitHub 的同步工具")
    ap.add_argument("action", choices=["push", "pull", "status", "login"],
                    help="push=提交并推送; pull=拉取更新; status=只看状态; login=保存 GitHub 凭据")
    ap.add_argument("-m", "--message", default=DEFAULT_MESSAGE, help="提交信息(push 用)")
    ap.add_argument("--yes", action="store_true", help="跳过确认(启动器/自动化用)")
    args = ap.parse_args(argv)

    if args.action == "push":
        return cmd_push(args.message, args.yes)
    if args.action == "pull":
        return cmd_pull(args.yes)
    if args.action == "login":
        return cmd_login()
    return cmd_status()


if __name__ == "__main__":
    raise SystemExit(main())

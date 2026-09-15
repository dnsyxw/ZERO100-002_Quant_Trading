"""`tools/git_sync.py`(与 GitHub 同步)的回归测试。

守住的四条底线 —— 每一条都对应一次真实踩坑或一次不可逆风险:

1. **默认不做不可逆动作**: 远端有本地没有的提交时必须**拒绝推送**(绝不强推覆盖);
   本地有未提交改动时 `pull` 必须**拒绝**(绝不自动 stash 藏起用户的改动)。
2. **不写脏状态**: `status` 是纯只读的, 跑完不许有任何 `add` / `commit` / `push` / `pull`。
3. **采集输出不依赖管道**: 本机沙箱禁止 `CreatePipe`, 所以 `_exec_git` 必须自带
   "退回临时文件"的兜底。这里用 monkeypatch 把管道模式打断, 验证它确实能降级。
   (若哪天有人把 `_exec_git` 改回裸 `capture_output=True`, 这条会红。)
4. **远端地址与分支名对上**: 仓库换地址却忘了改脚本, 会让推送静默打到别处。

测试**不联网、不碰真实 git**: `_exec_git` 一律被替换成脚本化的假 git。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import git_sync  # noqa: E402

BRANCH = "main"


class FakeGit:
    """按命令前缀返回预设输出的假 git, 并记录所有被调用的命令。"""

    def __init__(self, ahead: int = 0, behind: int = 0, status: str = "",
                 ignored: str = "") -> None:
        self.ahead = ahead
        self.behind = behind
        self.status = status
        self.ignored = ignored
        self.calls: list[list[str]] = []
        self.remote_ok = True

    def __call__(self, args: list[str]) -> tuple[int, str]:
        self.calls.append(list(args))
        joined = " ".join(args)

        if args[:1] == ["--version"]:
            return 0, "git version 2.55.0.windows.1\n"
        if joined == "rev-parse --is-inside-work-tree":
            return 0, "true\n"
        if joined == "remote get-url origin":
            return 0, git_sync.REPO_URL + "\n"
        if args[0] == "fetch":
            return (0, "") if self.remote_ok else (255, "boom: nothing to fetch\n")
        if joined == "rev-parse --abbrev-ref HEAD":
            return 0, BRANCH + "\n"
        if args[:2] == ["log", "--oneline"]:
            return 0, "abc1234 假提交\n"
        if joined == "status --porcelain":
            return 0, self.status
        if joined == "status --porcelain --ignored":
            return 0, self.ignored
        if args[:2] == ["rev-list", "--count"]:
            count = self.ahead if args[2].startswith(f"origin/{BRANCH}") else self.behind
            return 0, f"{count}\n"
        if args[:2] == ["diff", "--cached"]:
            return 0, "tools/demo.py\n"
        return 0, "(ok)\n"

    def ran(self, *prefix: str) -> bool:
        """是否调用过以 prefix 开头的命令。"""
        return any(call[:len(prefix)] == list(prefix) for call in self.calls)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGit:
    """把 git_sync 的 git 执行器换成假实现。"""
    stub = FakeGit()
    monkeypatch.setattr(git_sync, "_exec_git", stub)
    return stub


class Test常量:
    def test_分支与仓库地址写死且正确(self):
        assert git_sync.BRANCH == "main"
        assert git_sync.REPO_URL == "https://github.com/dnsyxw/ZERO100-002_Quant_Trading.git"

    def test_仓库根定位到项目根目录(self):
        # tools/git_sync.py -> parents[1] 应该是仓库根(有 启动.bat 与 .gitignore)
        assert (git_sync.ROOT / "启动.bat").exists()
        assert (git_sync.ROOT / ".gitignore").exists()


class TestStatus只读:
    def test_status不产生任何写操作(self, fake: FakeGit, capsys: pytest.CaptureFixture):
        assert git_sync.cmd_status() == 0
        for verb in (["add"], ["commit"], ["push"], ["pull"], ["stash"]):
            assert not fake.ran(*verb), f"status 不该调用 git {verb[0]}"
        out = capsys.readouterr().out
        assert "同步状态" in out

    def test_连不上远端时仍能报出状态(self, fake: FakeGit, capsys: pytest.CaptureFixture):
        fake.remote_ok = False
        assert git_sync.cmd_status() == 0
        assert "连不上远端" in capsys.readouterr().out


class Test推送拒绝危险路径:
    def test_远端有新提交时拒绝推送(self, fake: FakeGit, capsys: pytest.CaptureFixture):
        fake.behind = 3
        assert git_sync.cmd_push("msg", assume_yes=True) == 1
        assert not fake.ran("push")
        assert not fake.ran("commit")
        out = capsys.readouterr().out
        assert "远端有 3 个本地没有的提交" in out

    def test_连不上远端时拒绝推送(self, fake: FakeGit, capsys: pytest.CaptureFixture):
        fake.remote_ok = False
        assert git_sync.cmd_push("msg", assume_yes=True) == 1
        assert not fake.ran("push")

    def test_没有任何改动时不提交(self, fake: FakeGit, capsys: pytest.CaptureFixture):
        assert git_sync.cmd_push("msg", assume_yes=True) == 0
        assert not fake.ran("commit")
        assert "本地与远端一致" in capsys.readouterr().out

    def test_无改动但领先时只推送不新提交(self, fake: FakeGit):
        fake.ahead = 2
        assert git_sync.cmd_push("msg", assume_yes=True) == 0
        assert fake.ran("push")
        assert not fake.ran("commit")


class Test推送正常路径:
    def test_有改动时先提交再推送(self, fake: FakeGit, capsys: pytest.CaptureFixture):
        fake.status = " M tools/demo.py\n?? tools/new.py\n"
        assert git_sync.cmd_push("同步: 测试", assume_yes=True) == 0
        assert fake.ran("add", "-A")
        assert fake.ran("commit")
        assert fake.ran("push", "origin", f"{BRANCH}:{BRANCH}")
        out = capsys.readouterr().out
        assert "已推送到 GitHub" in out

    def test_未确认时不提交也不推送(self, fake: FakeGit, monkeypatch: pytest.MonkeyPatch):
        fake.status = " M tools/demo.py\n"
        monkeypatch.setattr("builtins.input", lambda *_: "n")
        assert git_sync.cmd_push("msg", assume_yes=False) == 1
        assert not fake.ran("commit")
        assert not fake.ran("push")


class Test拉取:
    def test_本地有未提交改动时拒绝拉取(self, fake: FakeGit, capsys: pytest.CaptureFixture):
        fake.behind = 2
        fake.status = " M tools/demo.py\n"
        assert git_sync.cmd_pull(assume_yes=True) == 1
        assert not fake.ran("pull")
        assert "未提交的改动" in capsys.readouterr().out

    def test_已是最新时不拉取(self, fake: FakeGit, capsys: pytest.CaptureFixture):
        assert git_sync.cmd_pull(assume_yes=True) == 0
        assert not fake.ran("pull")
        assert "已经是最新" in capsys.readouterr().out

    def test_落后时快进拉取(self, fake: FakeGit):
        fake.behind = 4
        assert git_sync.cmd_pull(assume_yes=True) == 0
        assert fake.ran("pull", "--ff-only", "origin", BRANCH)


class Test输出采集兜底:
    def test_管道被禁时退回临时文件(self, monkeypatch: pytest.MonkeyPatch):
        """沙箱禁 CreatePipe 时, _exec_git 必须降级到文件重定向而不是崩掉。"""
        import subprocess as real_subprocess

        def no_pipe(*_args, **_kwargs):
            raise OSError(5, "CreatePipe denied")

        monkeypatch.setattr(real_subprocess, "run", no_pipe)
        code, out = git_sync._exec_git(["--version"])
        assert code == 0
        assert "git version" in out

    def test_中文文件名不被转义(self, monkeypatch: pytest.MonkeyPatch):
        """core.quotepath=false 必须真的传下去, 否则中文文件名打出来是八进制串。"""
        import subprocess as real_subprocess

        seen: dict[str, list[str]] = {}

        class _Result:
            returncode = 0
            stdout = " M 启动.bat\n"
            stderr = ""

        def spy(cmd, *_args, **_kwargs):
            seen["cmd"] = list(cmd)
            return _Result()

        monkeypatch.setattr(real_subprocess, "run", spy)
        git_sync._exec_git(["status", "--porcelain"])
        assert seen["cmd"][:3] == ["git", "-c", "core.quotepath=false"]


class TestCLI:
    def test_非法动作被argparse拒绝(self):
        with pytest.raises(SystemExit):
            git_sync.main(["frobnicate"])

    def test_status动作可跑通(self, fake: FakeGit):
        assert git_sync.main(["status"]) == 0

    def test_push动作可跑通(self, fake: FakeGit):
        fake.status = " M tools/demo.py\n"
        assert git_sync.main(["push", "--yes", "-m", "同步: 测试"]) == 0

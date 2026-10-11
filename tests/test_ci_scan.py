"""ci_scan 的事件范围解析回归：固定事件 SHA、不可验证即阻断。

用**临时 Git 仓库**构造真实提交 DAG（线性推送、首次推送、强推、删除分支、
PR 合流），不依赖网络与工作区状态。扫描器本体（密钥/编号）另有既有用例覆盖，
这里验证的是范围：解析结果、提交集合与退出码承诺。
"""

import json
import subprocess
from pathlib import Path

import pytest

from scripts import ci_scan

ZERO = "0" * 40


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """线性两提交仓库：first → second（master）。"""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "master")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "first")
    (root / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "second")
    return root


def _rev(repo: Path, ref: str = "HEAD") -> str:
    return _git(repo, "rev-parse", ref)


class TestPushPlans:
    def test_normal_push_uses_event_shas(self, repo: Path) -> None:
        first, second = _rev(repo, "HEAD~1"), _rev(repo)
        plan = ci_scan.resolve_plan(
            "push", {"before": first, "after": second, "deleted": False}, cwd=repo
        )
        assert plan is not None
        assert plan.reason == "push"
        assert plan.head == second
        assert plan.content_base == first
        assert plan.message_base == first

    def test_first_push_scans_empty_tree_and_all_ancestors(self, repo: Path) -> None:
        second = _rev(repo)
        plan = ci_scan.resolve_plan(
            "push", {"before": ZERO, "after": second, "deleted": False}, cwd=repo
        )
        assert plan is not None
        assert plan.reason == "first-push"
        assert plan.message_base is None, "首次推送要扫 head 的全部祖先（含根提交）"
        # 空树对象必须真实可解析（不是拿常量当假设）
        tree = _git(repo, "rev-parse", f"{plan.content_base}^{{tree}}")
        assert tree == plan.content_base

    def test_before_equals_after_is_valid_empty_range(self, repo: Path) -> None:
        head = _rev(repo)
        plan = ci_scan.resolve_plan(
            "push", {"before": head, "after": head, "deleted": False}, cwd=repo
        )
        assert plan is not None
        assert plan.content_base == head and plan.message_base == head

    def test_deleted_branch_is_noop(self, repo: Path) -> None:
        before = _rev(repo)
        assert (
            ci_scan.resolve_plan(
                "push", {"before": before, "after": ZERO, "deleted": True}, cwd=repo
            )
            is None
        )

    def test_all_zero_after_without_deleted_flag_blocks(self, repo: Path) -> None:
        before = _rev(repo)
        with pytest.raises(ci_scan.ScanUnavailable):
            ci_scan.resolve_plan(
                "push", {"before": before, "after": ZERO, "deleted": False}, cwd=repo
            )

    @pytest.mark.parametrize(
        "payload",
        [
            {"before": "not-a-sha", "after": None},          # 非法 SHA 形态
            {"before": "deadbeef" * 5, "after": None},       # 合法形态但对象不存在
        ],
    )
    def test_invalid_shas_block(self, repo: Path, payload: dict) -> None:
        payload = dict(payload)
        payload["after"] = payload["after"] or _rev(repo)
        with pytest.raises(ci_scan.ScanUnavailable):
            ci_scan.resolve_plan("push", payload, cwd=repo)

    def test_missing_before_without_remote_blocks(self, repo: Path) -> None:
        """非零 before 取不到且精确 fetch 失败 → 阻断（不静默全量、不静默通过）。

        没有 origin 时 fetch 必然失败，正好覆盖「不可恢复」分支。
        """
        unknown = "1" * 40
        with pytest.raises(ci_scan.ScanUnavailable, match="fetch"):
            ci_scan.resolve_plan(
                "push", {"before": unknown, "after": _rev(repo)}, cwd=repo
            )


class TestPullRequestPlans:
    def test_pr_uses_merge_base_of_event_shas(self, repo: Path) -> None:
        base = _rev(repo)
        _git(repo, "checkout", "-qb", "feature")
        (repo / "b.txt").write_text("x\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "feat")
        head = _rev(repo)
        plan = ci_scan.resolve_plan(
            "pull_request",
            {"pull_request": {"base": {"sha": base}, "head": {"sha": head}}},
            cwd=repo,
        )
        assert plan is not None
        assert plan.reason == "pr"
        assert plan.content_base == base, "内容基线是 merge-base，不是 base 本身之外的东西"
        assert plan.message_base == base

    def test_pr_with_multiple_merge_bases_blocks(self, repo: Path) -> None:
        """criss-cross 合流会产生多个 merge-base：范围不唯一，必须拒绝而不是挑一个。

        构图（经典 criss-cross）：共同祖先上分别出「主线一次提交」与「侧分支一次提交」，
        然后主线合侧分支、侧分支合主线——两边互不包含对方的合并提交，共同祖先因此有两个。
        『对方的最新 tip』，其中一侧就会包含另一侧，退化成唯一 merge-base。
        """
        base = _rev(repo)
        _git(repo, "checkout", "-qb", "side", base)
        (repo / "side.txt").write_text("s" + chr(10), encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "side change")
        s1 = _rev(repo)
        _git(repo, "checkout", "-q", "master")
        (repo / "main.txt").write_text("m" + chr(10), encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "main change")
        m1 = _rev(repo)
        # 主线先合侧分支那次提交；侧分支再合**主线那一次提交**（不是主线最新 tip，
        # 否则一侧包含另一侧，退化成唯一 merge-base）
        _git(repo, "merge", "-q", "--no-ff", "-m", "merge side", s1)
        _git(repo, "checkout", "-q", "side")
        _git(repo, "merge", "-q", "--no-ff", "-m", "merge main", m1)
        side_tip = _rev(repo)
        _git(repo, "checkout", "-q", "master")
        bases = _git(repo, "merge-base", "--all", "master", "side").splitlines()
        assert len(bases) >= 2, f"构图未产生多个 merge-base: {bases}"
        with pytest.raises(ci_scan.ScanUnavailable, match="多个 merge-base"):
            ci_scan.resolve_plan(
                "pull_request",
                {"pull_request": {"base": {"sha": _rev(repo)}, "head": {"sha": side_tip}}},
                cwd=repo,
            )

    def test_pr_without_common_ancestor_blocks(self, repo: Path) -> None:
        """孤立历史：merge-base 取不到即阻断，不擅自选一个基线。"""
        orphan = _rev(repo)
        _git(repo, "checkout", "-q", "--orphan", "island")
        _git(repo, "rm", "-rf", "--cached", ".")
        (repo / "c.txt").write_text("c\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "island")
        island = _rev(repo)
        with pytest.raises(ci_scan.ScanUnavailable):
            ci_scan.resolve_plan(
                "pull_request",
                {"pull_request": {"base": {"sha": orphan}, "head": {"sha": island}}},
                cwd=repo,
            )


class TestEventPayloadEdges:
    def test_missing_fields_block(self, repo: Path) -> None:
        with pytest.raises(ci_scan.ScanUnavailable):
            ci_scan.resolve_plan("push", {}, cwd=repo)
        with pytest.raises(ci_scan.ScanUnavailable):
            ci_scan.resolve_plan("pull_request", {"pull_request": {}}, cwd=repo)

    def test_unsupported_event_blocks(self, repo: Path) -> None:
        with pytest.raises(ci_scan.ScanUnavailable, match="不支持的事件类型"):
            ci_scan.resolve_plan("workflow_dispatch", {}, cwd=repo)


class TestMainExitCodes:
    def _write_event(self, tmp_path: Path, name: str, payload: dict) -> Path:
        path = tmp_path / "event.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_missing_event_env_returns_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
        monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
        assert ci_scan.main([]) == 2

    def test_unreadable_payload_returns_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
        monkeypatch.setenv("GITHUB_EVENT_PATH", str(tmp_path / "missing.json"))
        assert ci_scan.main([]) == 2

    def test_unverifiable_range_returns_2_and_does_not_scan(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        event = self._write_event(tmp_path, "push", {"before": "1" * 40, "after": _rev(repo)})
        monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
        monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
        assert ci_scan.main(["--repo", str(repo)]) == 2

    def test_deleted_branch_returns_0(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        event = self._write_event(
            tmp_path, "push", {"before": _rev(repo), "after": ZERO, "deleted": True}
        )
        monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
        monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
        assert ci_scan.main(["--repo", str(repo)]) == 0

    def test_empty_range_dry_run_returns_0(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """before == after：合法空范围，dry-run 不执行扫描也不报错。"""
        head = _rev(repo)
        event = self._write_event(
            tmp_path, "push", {"before": head, "after": head, "deleted": False}
        )
        monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
        monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
        assert ci_scan.main(["--repo", str(repo), "--dry-run"]) == 0

"""CI 事件范围解析与统一扫描编排。

为什么单独成脚本：原 workflow 用「推送载荷里 commits 数组的长度」推算基线
（`before="HEAD~$#"`），但那个长度**不等于第一父链步数**——含合并的推送会错位，
首次推送的根提交会越界，载荷截断或为空时 HEAD~0 直接扫了个空 diff 还报成功。
这里改为只认事件载荷里的固定 SHA，范围不可验证时明确失败（退出码 2 阻断）。

范围契约（与 .github/workflows/quality-gates.yml 的调用一一对应）：

| 事件 | 文件内容比较 | 提交信息集合 |
|---|---|---|
| PR | merge-base(base, head) 树 → 事件 head | reachable(head) − reachable(base) |
| 普通推送 | before → after | reachable(after) − reachable(before) |
| 非快进推送（before 可读） | before → after | 同上 |
| 首次推送（before 全零） | 空树 → after | after 的全部祖先（已知空边界） |
| before 非零但本地没有 | 精确 fetch 该 SHA 后按上面处理 | 同上 |
| before 非零且取不到 | 不执行 | 不执行（返回 2） |

exit code：0 = 全部通过；1 = 有命中；2 = 扫描未执行（拒绝放行）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# CI Windows runner 默认 stdout 为 cp1252，中文输出会 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001, S110
        pass

REPO = Path(__file__).resolve().parent.parent

#: 空树对象：git 的对象格式无关常量（sha1 仓库）；用 rev-parse 取更稳，
#: 但这枚 SHA 在测试仓库里也可用，故同时保留常量与解析兜底。
EMPTY_TREE_SHA1 = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
ZERO_SHA = "0" * 40


class ScanUnavailable(RuntimeError):
    """范围不可验证：调用方据此返回 2（拒绝放行，不宣称已扫）。"""


@dataclass(frozen=True)
class ScanPlan:
    """一次 CI 扫描的确定范围。"""

    head: str
    content_base: str  # 规范 commit/tree OID（首次推送时为空树对象）
    message_base: str | None  # None = head 的全部祖先
    reason: str  # pr / push / force-push / first-push


def _git(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd or Path.cwd()),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _git_ok(args: list[str], *, cwd: Path | None = None) -> str:
    proc = _git(args, cwd=cwd)
    if proc.returncode != 0:
        raise ScanUnavailable(f"git {' '.join(args)} 失败：{(proc.stderr or '').strip()[:200]}")
    return proc.stdout


def _canonical_oid(sha: str, *, what: str, cwd: Path | None = None) -> str:
    """校验十六进制 SHA 并解析为仓库中的规范 commit OID。"""
    if not isinstance(sha, str) or not sha:
        raise ScanUnavailable(f"{what} 缺失")
    cleaned = sha.strip()
    if len(cleaned) not in (40, 64) or any(ch not in "0123456789abcdefABCDEF" for ch in cleaned):
        raise ScanUnavailable(f"{what} 不是合法十六进制 SHA：{sha!r}")
    proc = _git(["rev-parse", "--verify", f"{cleaned}^{{commit}}"], cwd=cwd)
    if proc.returncode != 0:
        raise ScanUnavailable(f"{what} 不是可读取的 commit：{cleaned}")
    return proc.stdout.strip()


def _is_zero(sha: str) -> bool:
    return bool(sha) and set(sha) == {"0"}


def _empty_tree(*, cwd: Path | None = None) -> str:
    proc = _git(["rev-parse", "--verify", f"{EMPTY_TREE_SHA1}^{{tree}}"], cwd=cwd)
    if proc.returncode == 0:
        return proc.stdout.strip()
    return EMPTY_TREE_SHA1


def _fetch_missing(sha: str, *, cwd: Path | None = None) -> bool:
    """精确 fetch 事件里的那个 SHA（不用浮动分支位置猜旧提交）。"""
    for args in (
        ["fetch", "--no-tags", "--quiet", "origin", sha],
        ["fetch", "--no-tags", "--quiet", "--depth", "1", "origin", sha],
    ):
        if _git(args, cwd=cwd).returncode == 0:
            probe = _git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=cwd)
            if probe.returncode == 0:
                return True
    return False


def _merge_base(base: str, head: str, *, cwd: Path | None = None) -> str:
    """唯一 merge-base；没有或不止一个都拒绝（不擅自选一个）。

    `--all` 是必需的：criss-cross 合流（两边互相合过）会有多个 merge-base，
    单值调用会静默挑一个，扫描范围随之变成「其中一个基线」，与契约不符。
    """
    proc = _git(["merge-base", "--all", base, head], cwd=cwd)
    if proc.returncode != 0:
        raise ScanUnavailable(f"找不到 {base} 与 {head} 的共同祖先（不擅自选一个）")
    bases = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not bases:
        raise ScanUnavailable(f"{base} 与 {head} 没有 merge-base")
    if len(bases) > 1:
        raise ScanUnavailable(
            f"{base} 与 {head} 有多个 merge-base（{[b[:12] for b in bases]}）——范围不唯一，拒绝放行"
        )
    return bases[0]


def resolve_plan(
    event_name: str, payload: dict, *, cwd: Path | None = None
) -> ScanPlan | None:
    """把事件载荷解析为确定的扫描范围；None = 已确认无需扫描（删除事件）。"""
    if event_name == "pull_request":
        pull = payload.get("pull_request") or {}
        base_sha = str((pull.get("base") or {}).get("sha") or "")
        head_sha = str((pull.get("head") or {}).get("sha") or "")
        head = _canonical_oid(head_sha, what="pull_request.head.sha", cwd=cwd)
        if _is_zero(base_sha):
            raise ScanUnavailable("PR 的 base.sha 是全零")
        try:
            base = _canonical_oid(base_sha, what="pull_request.base.sha", cwd=cwd)
        except ScanUnavailable:
            # 基分支可能被强推过：先精确 fetch 再解析；仍失败即阻断
            if not _fetch_missing(base_sha, cwd=cwd):
                raise ScanUnavailable(
                    f"PR 基线 {base_sha} 在本地取不到且精确 fetch 失败"
                ) from None
            base = _canonical_oid(base_sha, what="pull_request.base.sha", cwd=cwd)
        merge_base = _merge_base(base, head, cwd=cwd)
        return ScanPlan(
            head=head, content_base=merge_base, message_base=base, reason="pr"
        )
    if event_name == "push":
        before = str(payload.get("before") or "")
        after = str(payload.get("after") or "")
        deleted = bool(payload.get("deleted"))
        if _is_zero(after):
            if deleted:
                return None  # 删除分支：没有新增树、没有新增提交
            raise ScanUnavailable("push 事件的 after 是全零但未标记 deleted")
        head = _canonical_oid(after, what="after", cwd=cwd)
        if _is_zero(before):
            # 首次推送：已知空边界——全树内容 + head 的全部祖先（含根提交）
            return ScanPlan(
                head=head, content_base=_empty_tree(cwd=cwd), message_base=None, reason="first-push"
            )
        try:
            base = _canonical_oid(before, what="before", cwd=cwd)
            reason = "push"
        except ScanUnavailable:
            if not _fetch_missing(before, cwd=cwd):
                raise ScanUnavailable(
                    f"push 基线 {before} 在本地取不到且精确 fetch 失败（不可恢复，需先恢复该对象）"
                ) from None
            base = _canonical_oid(before, what="before", cwd=cwd)
            reason = "force-push"
        if base == head:
            # before == after：合法空范围
            return ScanPlan(head=head, content_base=base, message_base=base, reason=reason)
        return ScanPlan(head=head, content_base=base, message_base=base, reason=reason)
    raise ScanUnavailable(f"不支持的事件类型: {event_name}")


def _run_scanner(args: list[str], *, cwd: Path | None = None) -> int:
    proc = subprocess.run(
        [sys.executable, *args],
        cwd=str(cwd or REPO),
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return proc.returncode


def run_plan(plan: ScanPlan, *, cwd: Path | None = None) -> int:
    """按计划跑三项扫描并汇总退出码：任一 2 → 2；否则有 1 → 1；全 0 → 0。"""
    base, head = plan.content_base, plan.head
    results: list[tuple[str, int]] = []
    results.append(
        (
            "密钥扫描",
            _run_scanner(["scripts/secret_scan.py", "--base", base, "--head", head], cwd=cwd),
        )
    )
    results.append(
        (
            "编号引用扫描",
            _run_scanner(["scripts/forbidden_refs.py", "--base", base, "--head", head], cwd=cwd),
        )
    )
    if plan.message_base is None:
        results.append(
            (
                "提交信息扫描",
                _run_scanner(["scripts/forbidden_refs.py", "--commits", "--head", head], cwd=cwd),
            )
        )
    else:
        results.append(
            (
                "提交信息扫描",
                _run_scanner(
                    [
                        "scripts/forbidden_refs.py",
                        "--commits",
                        "--base",
                        plan.message_base,
                        "--head",
                        head,
                    ],
                    cwd=cwd,
                ),
            )
        )
    for name, code in results:
        print(f"[ci-scan] {name}: 退出码 {code}", flush=True)
    if any(code == 2 for _, code in results):
        return 2
    if any(code == 1 for _, code in results):
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CI 事件范围扫描（固定事件 SHA，范围不可验证即阻断）")
    parser.add_argument("--event-name", default=None, help="缺省读 GITHUB_EVENT_NAME")
    parser.add_argument("--event-path", default=None, help="缺省读 GITHUB_EVENT_PATH")
    parser.add_argument("--dry-run", action="store_true", help="只解析范围并打印，不执行扫描")
    parser.add_argument("--repo", default=None, help="仓库根（缺省为脚本所在仓库；测试用）")
    args = parser.parse_args(argv)

    cwd = Path(args.repo) if args.repo else REPO
    event_name = args.event_name or os.environ.get("GITHUB_EVENT_NAME", "")
    event_path = args.event_path or os.environ.get("GITHUB_EVENT_PATH", "")
    if not event_name or not event_path:
        print(
            "[ci-scan] 缺少事件信息（GITHUB_EVENT_NAME / GITHUB_EVENT_PATH）——拒绝放行",
            file=sys.stderr,
        )
        return 2
    try:
        payload = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"[ci-scan] 事件载荷读取失败：{error}", file=sys.stderr)
        return 2
    try:
        plan = resolve_plan(event_name, payload, cwd=cwd)
    except ScanUnavailable as error:
        print(f"[ci-scan] 范围无法验证，拒绝放行：{error}", file=sys.stderr)
        return 2
    if plan is None:
        print(f"[ci-scan] {event_name}：事件已确认无需扫描（删除分支），通过")
        return 0
    print(
        f"[ci-scan] {event_name}（{plan.reason}）：head={plan.head[:12]} "
        f"content_base={plan.content_base[:12]} message_base={(plan.message_base or 'ALL')[:12]}"
    )
    if args.dry_run:
        return 0
    return run_plan(plan, cwd=cwd)


if __name__ == "__main__":
    raise SystemExit(main())

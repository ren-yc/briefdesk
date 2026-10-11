"""编号引用防回归门禁：禁止注释 / 文档 / 提交信息出现仓库外不可访问的编号指针。

规则与豁免见 scripts/forbidden_refs.py 模块 docstring。存量（审查报告条目号、
计划产物编号、流水号批次）已分批清理完毕，此处采用全库 **0 容忍**：任何命中
即失败。

为什么放在测试里而不是只靠钩子：钩子可被 --no-verify 绕过，CI 也只在 push 时跑；
纳入 pytest 后本地门禁与 CI 的三版本矩阵都会覆盖，且与 test_docs_anchors.py /
test_icon_manifest.py 同属「仓库不变量」测试。

注意：本文件的自证样例在运行期拼接编号（见 _ref），避免测试自身被规则命中。
"""

import subprocess
from pathlib import Path

import pytest

# 与 tests/test_fetch_icons_script.py 同口径：tests/ 为包，仓库根已在 sys.path 上
from scripts.forbidden_refs import (
    _added_lines,
    scan_commit_message,
    scan_commit_range,
    scan_lines,
    scan_source,
    scan_tree,
    strip_git_commentary,
    tracked_files,
)

_ROOT = Path(__file__).resolve().parents[1]

# 增量扫描用例：diff 与文件内容是两路输入——diff 给行号，内容取自
# 「新增侧」版本。用仓库内已跟踪的文件名，配合打桩内容精确验证定位逻辑。
_DIFF_FILE = "scripts/secret_scan.py"

_DIFF_HEADER = f"""diff --git a/{_DIFF_FILE} b/{_DIFF_FILE}
--- a/{_DIFF_FILE}
+++ b/{_DIFF_FILE}
"""


def _ref(letter: str, number: int) -> str:
    """运行期拼接条目码：样例本身不能以字面量出现在源码里（否则被自身规则命中）。"""
    return f"{letter}{number}"


def test_diff_scan_ignores_unchanged_old_lines() -> None:
    """未改动的旧行不在新增行集合内：即使同文件别处有编号，也不得报出。"""
    source = "\n".join(
        [
            f"# 复核 {_ref('P', 1)}-1 示例",
            "# 本轮新增的无害注释",
        ]
    )
    assert scan_lines("tests/test_db.py", source, {2}) == []


def test_diff_scan_honours_diff_line_numbers() -> None:
    """命中行号取自 diff 的新增侧（行号与内容两路输入，各司其职）。"""
    source = "\n".join([f"# 复核 {_ref('P', 1)}-1 示例", "# 本轮新增的无害注释"])
    hits = scan_lines("tests/test_db.py", source, {1, 2})
    assert len(hits) == 1
    assert hits[0].line == 1


def test_diff_parser_maps_added_lines_to_new_file() -> None:
    """diff 解析：新增行的行号按新文件计数（本用例只测解析器本身）。"""
    diff = _DIFF_HEADER + "@@ -10,0 +12,2 @@\n+第一行\n+第二行\n"
    assert _added_lines(diff) == {_DIFF_FILE: {12, 13}}

def test_no_plan_references_anywhere() -> None:
    """全库注释 / 文档不得出现编号引用（审查报告条目号、计划产物编号、流水号批次）。"""
    hits = scan_tree(_ROOT)
    if hits:
        detail = "\n".join(f"  {hit.path}:{hit.line} [{hit.rule}] {hit.text[:120]}" for hit in hits)
        pytest.fail(
            "检测到编号引用（指向仓库外不可访问的文档，读者无法还原上下文）：\n"
            f"{detail}\n"
            "请改为说明「为什么」与失败模式，并把回归位置指向仓库内的测试或代码。"
        )


_CASES: list[tuple[str, bool]] = [
    # 规则 1：组合编号（中文方括号 + 序号 + 条目码）
    (f"# 【2·{_ref('P', 1)}】示例", True),
    # 规则 2：归因信号词 + 编号（含 1-2 字母码与裸数字）
    (f"# 复核 {_ref('PQ', 1)}-1：示例", True),
    ("# 审计 #12：示例", True),
    # 规则 3：条目码
    (f"# {_ref('XY', 3)} 说明", True),
    # 规则 4：单字母码 + 冒号（注释语境）
    (f"# {_ref('F', 5)}：示例", True),
    # 规则 5：计划 / 会话产物路径
    ("# 见 PLAN-XY.md", True),
    # 规则 6：流水号批次
    ("# 第一批改动", True),
    # 豁免：编码名 / RFC 编号 / 控制字符（规则文本本身不得自我命中）
    ("# 统一 UTF-8 编码，避免 U+FFFD", False),
    ("# 非 ASCII 基名走 RFC 5987 扩展", False),
    ("# 校验 sha256 摘要", False),
    ("# 去除 C0 控制字符", False),
    # 豁免：静态检查码（noqa）
    ("# noqa: F401 保留 re-export", False),
    ("# ruff: noqa: I001 — 导入顺序即组装顺序", False),
    # 豁免：分类 / 判重指标名（F 值）语境
    ("# accuracy / precision / recall / F1 + 混淆矩阵", False),
    ("# 精确率与召回率的调和平均 F1", False),
    # 豁免：可跟踪的 issue 编号（无归因信号词时不命中）
    ("# 关联 Fixes #123 的修复", False),
    # 豁免：行内显式标记
    (f"# {_ref('XY', 3)} allow-plan-ref 保留理由", False),
    # 归因词后接中文量词属于自然行文，不构成编号引用
    ("# 审计第三次发现该问题", False),
    # 归因词后接四位年份：正则不得截断年份绕过量词排除
    ("# 安全审计 2026 年复查", False),
    # 常见技术缩写与条目码同形，白名单放过
    ("# 用 ES6 模块语法与 MD5 摘要，P95 延迟", False),
    # 豁免是剥离片段而非整行放行：同行夹带的真实编号仍须命中
    (f"# noqa: F401 —— 见复核 {_ref('P', 2)}-18", True),
    (f"# 精确率与召回率的调和平均 F1，另见 {_ref('F', 2)}", True),
    (f"# 参考 RFC 5987 与 {_ref('H', 2)}", True),
    # 普通注释
    ("# 关闭后拒绝重建连接，防止残余任务复活", False),
]


@pytest.mark.parametrize(("text", "expected"), _CASES)
def test_rule_matching(text: str, expected: bool) -> None:
    """规则与豁免的自证：样例必须命中，豁免样例必须放过。"""
    assert bool(scan_source("<sample>.py", text)) is expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (f"fix(ui): 修复浮层焦点栈与断线可见性（{_ref('P', 2)}-30 第一批）", True),
        ("fix(sources): 排期7b——翻页早停与 404 容错", True),
        # git 注释行与 scissors 之后的 diff 不会进入提交，不得据此拦截
        (
            (
                "fix(x): 说明\n\n# 第 3 批 模板注释\n"
                "# ------------------------ >8 ------------------------\n"
                f"diff --git a/x.js b/x.js\n+// 复核 {_ref('P', 1)}-1\n"
            ),
            False,
        ),
        ("fix(dedup): 嵌入截断防毒丸，缺失向量降级字符重叠", False),
        ("feat(ai): 支持通过 AI_DISABLE_THINKING 禁用思考模式", False),
    ],
)
def test_commit_message_matching(message: str, expected: bool) -> None:
    """提交信息（subject）同样禁止流水号与编号指针。"""
    assert bool(scan_commit_message(message)) is expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # 行内尾注释此前完全不扫描
        (f"let a = 1; // 复核 {_ref('P', 1)}-1", True),
        (f"fetch('http://a'); // 见 {_ref('F', 2)}", True),
        # 字符串里的 // 与 URL 的 :// 不是注释起点
        ('const u = "http://x//y"; // 普通注释', False),
        ("let b = 2; // 普通注释", False),
    ],
)
def test_js_trailing_comment_is_scanned(line: str, expected: bool) -> None:
    """JS 代码行末尾的 // 注释与整行注释同口径。"""
    assert bool(scan_source("<sample>.js", line)) is expected


def test_strip_git_commentary_keeps_only_message_body() -> None:
    raw = "subject\n\nbody\n# comment\n# ------------------------ >8 ------------------------\ndiff\n"
    assert strip_git_commentary(raw) == "subject\n\nbody"


def test_git_failure_refuses_to_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """git 取不到差异时必须退出码 2（扫描未执行），不得当作空 diff 放行。

    此前 _git 抛 RuntimeError 直上 main 之外，靠 traceback 以退 1 结束；
    语义上与「命中」不可区分，也让调用方无法判断「扫描没跑」。与
    scripts/secret_scan.py 同口径。
    """
    from scripts import forbidden_refs

    def _boom(args: list[str], *, cwd: Path | None = None) -> str:
        raise RuntimeError("git diff 失败")

    monkeypatch.setattr(forbidden_refs, "_git", _boom)
    assert forbidden_refs.main([]) == 2


def _commit_range(base: str) -> list[str]:
    """返回 base..HEAD 的提交 sha 列表（无提交时为空）。"""
    out = subprocess.run(
        ["git", "log", "--format=%H", f"{base}..HEAD"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return out.stdout.split()


def _commit_message(sha: str) -> str:
    out = subprocess.run(
        ["git", "show", "-s", "--format=%B", sha],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return out.stdout


def test_local_commit_messages_are_clean() -> None:
    """本地领先 origin/master 的提交信息不得含编号引用。

    commit-msg 钩子覆盖提交前，但可被 --no-verify 绕过，且直接推 master 的
    路径没有 CI 兜底（CI 侧的提交信息扫描见 quality-gates.yml 的对应 step）。
    基线不可得时 skip 并说明原因：浅克隆/新建仓库没有 origin/master，
    此时报错会逼人 --no-verify，比跳过更糟。
    """
    base = "origin/master"
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", base],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip(f"本地无 {base} 基线（浅克隆或新建仓库），跳过提交信息检查")

    offenders = [
        (sha[:8], hit)
        for sha in _commit_range(base)
        for hit in scan_commit_message(_commit_message(sha))
    ]
    if offenders:
        detail = "\n".join(
            f"  {sha} [{hit.rule}] {hit.text[:120]}" for sha, hit in offenders
        )
        pytest.fail(
            "本地提交信息含编号引用（仓库外读者无法据此还原上下文）：\n"
            f"{detail}\n"
            "请用「行为变化」描述取代，勿写审查报告条目号 / 计划产物编号 / 流水号。"
        )


def test_tracked_files_independent_of_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    """全量扫描的 git ls-files 须在仓库根执行：从子目录跑 pytest 时路径拼接才正确。

    此前在 cwd 执行，从 tests/ 目录运行会得到相对 tests/ 的路径，拼到根目录后
    文件不存在被静默跳过，全量扫描零命中「通过」——等于没扫。
    """
    monkeypatch.chdir(_ROOT / "tests")
    files = tracked_files(_ROOT)
    assert "scripts/forbidden_refs.py" in files
    assert all((_ROOT / f).exists() for f in files)

def _dual_endpoint_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """两提交临时仓库：返回 (repo, first, second)。自行构造，不动工作区。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args: str) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return proc.stdout.strip()
    git("init", "-q", "-b", "master")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (repo / "a.txt").write_text("one" + chr(10), encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "first")
    first = git("rev-parse", "HEAD")
    (repo / "a.txt").write_text("one" + chr(10) + "two" + chr(10), encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "second")
    return repo, first, git("rev-parse", "HEAD")


def test_commit_range_counts_only_new_commits(tmp_path: Path) -> None:
    """提交信息集合 = reachable(head) − reachable(base)；空集合是合法结果。"""
    repo, first, second = _dual_endpoint_repo(tmp_path)
    hits, count = scan_commit_range(first, second, cwd=repo)
    assert (hits, count) == ([], 1)
    _, all_count = scan_commit_range(None, second, cwd=repo)
    assert all_count == 2, "首次推送要覆盖 head 的全部祖先（含根提交）"
    _, empty = scan_commit_range(second, second, cwd=repo)
    assert empty == 0, "before == after 的合法空集合不算失败"


def test_commit_range_reports_hits(tmp_path: Path) -> None:
    """命中提交信息要能被枚举出来（而不是静默跳过）。"""
    repo, _first, second = _dual_endpoint_repo(tmp_path)
    def git(*args: str) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return proc.stdout.strip()
    (repo / "b.txt").write_text("b" + chr(10), encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", f"bad {_ref('P', 3)}-1 reference")
    third = git("rev-parse", "HEAD")
    hits, count = scan_commit_range(second, third, cwd=repo)
    assert count == 1
    assert hits and hits[0].path == "<commit-message>"


def test_commit_range_fails_loudly_on_bad_revision(tmp_path: Path) -> None:
    """枚举失败必须抛错（调用方转退出码 2），不得当作空集合通过。"""
    repo, _first, _second = _dual_endpoint_repo(tmp_path)
    with pytest.raises(RuntimeError):
        scan_commit_range("no-such-rev", "HEAD", cwd=repo)


# ── 新端源码读取失败的分流（扫描未执行 ≠ 文件不存在）──


def _repo_with_added_lines(tmp_path: Path) -> tuple[Path, str, str]:
    """双提交仓库：第二次提交新增一行，返回 (repo, first, second)。"""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=True,
        )
        return proc.stdout.strip()

    git("init", "-q")
    git("config", "user.email", "probe@example.invalid")
    git("config", "user.name", "probe")
    (repo / "a.txt").write_text("one" + chr(10), encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "first")
    first = git("rev-parse", "HEAD")
    (repo / "a.txt").write_text("one" + chr(10) + "two" + chr(10), encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "second")
    return repo, first, git("rev-parse", "HEAD")


def _patch_git_cwd(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """把模块内的 git 调用绑到临时仓库，并同步 _file_at_rev 的内部实现。"""
    from scripts import forbidden_refs

    def _git(args: list[str], *, cwd: Path | None = None) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd or repo), capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} 失败：{proc.stderr.strip()}")
        return proc.stdout

    monkeypatch.setattr(forbidden_refs, "_git", _git)


def test_scan_diff_raises_when_present_file_cannot_be_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """有新增行却读不到新端：必须抛错，不得静默跳过当作「没内容可扫」。

    桩返回 None（不是抛错）——抛错在「None 即跳过」的旧实现下同样会经 main
    的异常路径得到退出码 2，用例照样绿，等于零判别力。这里模拟的是读取本身
    失败（对象损坏 / 索引被改写）：新增行已经证明新端该有这个文件。
    """
    from scripts import forbidden_refs

    repo, first, second = _repo_with_added_lines(tmp_path)
    _patch_git_cwd(monkeypatch, repo)
    diff_text = subprocess.run(
        ["git", "diff", "--unified=0", first, second], cwd=repo, capture_output=True,
        text=True, encoding="utf-8", errors="replace", check=True,
    ).stdout
    assert "+two" in diff_text, "夹具必须产生新增行"
    monkeypatch.setattr(forbidden_refs, "_file_at_rev", lambda rev, path: None)
    with pytest.raises(RuntimeError, match="读不到"):
        forbidden_refs.scan_diff(diff_text, rev=None, staged=False, new_side=second)


def test_scan_diff_skips_pure_deletion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """纯删除：新端没有该文件，也没有新增行——合法跳过，不是扫描失败。"""
    from scripts import forbidden_refs

    repo, _first, second = _repo_with_added_lines(tmp_path)
    subprocess.run(["git", "rm", "-q", "a.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "delete"], cwd=repo, check=True)
    deleted = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True,
    ).stdout.strip()
    diff_text = subprocess.run(
        ["git", "diff", "--unified=0", second, deleted], cwd=repo, capture_output=True,
        text=True, encoding="utf-8", errors="replace", check=True,
    ).stdout
    assert forbidden_refs.scan_diff(diff_text, rev=None, staged=False, new_side=deleted) == []


def test_scan_diff_fails_closed_when_new_side_rev_is_invalid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """新端 rev 非法（基线引用失效等）：按扫描未执行抛错，不得判「没有该文件」。

    这条不桩 _file_at_rev，走的是真实的 git 失败路径——rev 取不到时「读不到」
    与「文件不存在」无法区分，只能失败关闭。
    """
    from scripts import forbidden_refs

    repo, first, second = _repo_with_added_lines(tmp_path)
    _patch_git_cwd(monkeypatch, repo)
    diff_text = subprocess.run(
        ["git", "diff", "--unified=0", first, second], cwd=repo, capture_output=True,
        text=True, encoding="utf-8", errors="replace", check=True,
    ).stdout
    assert "+two" in diff_text, "夹具必须产生新增行"
    with pytest.raises(RuntimeError, match="读不到"):
        forbidden_refs.scan_diff(diff_text, rev=None, staged=False, new_side="no-such-rev")


def test_main_maps_unreadable_new_side_to_exit_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """真实入口收口：新端读不到时退出码 2，且不得打印「未发现编号引用」。

    静默跳过会让 CI 显示扫描通过——门禁失守比扫不出来更糟。
    """
    from scripts import forbidden_refs

    repo, first, second = _repo_with_added_lines(tmp_path)
    _patch_git_cwd(monkeypatch, repo)
    monkeypatch.setattr(forbidden_refs, "_file_at_rev", lambda rev, path: None)
    assert forbidden_refs.main(["--base", first, "--head", second]) == 2
    captured = capsys.readouterr()
    assert "未发现编号引用" not in captured.out, "漏扫不得伪装成通过"


# ── 非 ASCII 路径：git 的引号/转义不得让文件被丢掉或读不到 ──


def _repo_with_non_ascii_paths(tmp_path: Path) -> tuple[Path, str, str]:
    """双提交仓库：非 ASCII 文件名与 ASCII 对照各新增一行命中，返回 (repo, first, second)。"""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=True,
        )
        return proc.stdout.strip()

    hit = f"# 复核 {_ref('P', 1)}-1 示例"
    git("init", "-q")
    git("config", "user.email", "probe@example.invalid")
    git("config", "user.name", "probe")
    for name in ("plain.py", "配置说明.py"):
        (repo / name).write_text("x = 1" + chr(10), encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "first")
    first = git("rev-parse", "HEAD")
    for name in ("plain.py", "配置说明.py"):
        (repo / name).write_text("x = 1" + chr(10) + hit + chr(10), encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "second")
    return repo, first, git("rev-parse", "HEAD")


def test_tracked_files_and_tree_scan_keep_non_ascii_paths(tmp_path: Path) -> None:
    """ls-files 默认给非 ASCII 路径加引号：后缀过滤与读取都不得因此丢掉该文件。

    引号形态既让 endswith(_TEXT_SUFFIXES) 失配，也让拼出的路径不存在——全量扫描会
    静默漏掉它：门禁看起来跑过，却没扫到这个文件。
    """
    from scripts import forbidden_refs

    repo, _first, _second = _repo_with_non_ascii_paths(tmp_path)
    names = forbidden_refs.tracked_files(repo)
    assert "配置说明.py" in names, f"非 ASCII 文件被后缀过滤丢掉：{names}"
    assert {hit.path for hit in forbidden_refs.scan_tree(repo)} == {"plain.py", "配置说明.py"}


def test_incremental_scan_keeps_non_ascii_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """增量扫描同样按原始路径：diff 头行的引号不得让新增行读不到（旧行为是退出 2）。"""
    from scripts import forbidden_refs

    repo, first, second = _repo_with_non_ascii_paths(tmp_path)
    monkeypatch.chdir(repo)
    diff_text = forbidden_refs._range_diff(first)
    assert "配置说明.py" in forbidden_refs._added_lines(diff_text), "diff 头行的路径被引号破坏"
    hits = forbidden_refs.scan_diff(diff_text, rev=None, staged=False, new_side=second)
    assert {hit.path for hit in hits} == {"plain.py", "配置说明.py"}


# ── 全量扫描：读不到的文件必须响亮，不得静默跳过 ──


def _repo_with_unreadable_text_files(tmp_path: Path) -> Path:
    """仓库：一个可读文件、一个 GBK 文本文件、一个跟踪后被从工作区删除的文件。"""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=True,
        )
        return proc.stdout.strip()

    git("init", "-q")
    git("config", "user.email", "probe@example.invalid")
    git("config", "user.name", "probe")
    (repo / "ok.py").write_text("x = 1" + chr(10), encoding="utf-8")
    (repo / "gbk.md").write_bytes("# 标题".encode("gbk"))
    (repo / "gone.py").write_text("y = 1" + chr(10), encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")
    (repo / "gone.py").unlink()
    return repo


def test_scan_tree_reports_unreadable_files_instead_of_skipping(tmp_path: Path) -> None:
    """非 UTF-8 / 工作区缺失的跟踪文件必须报出，不能静默跳过。

    静默跳过等于「跑过了但没扫」：scan_tree 返回 0 命中、入口退出 0，门禁看起来是
    绿的，而该文件的注释与文档正文从未被读过。
    """
    from scripts import forbidden_refs

    repo = _repo_with_unreadable_text_files(tmp_path)
    by_path = {hit.path: hit for hit in forbidden_refs.scan_tree(repo)}
    assert set(by_path) == {"gbk.md", "gone.py"}, f"读不到的文件必须被报出：{sorted(by_path)}"
    for rel in ("gbk.md", "gone.py"):
        assert by_path[rel].rule == "读取失败", by_path[rel]
        assert by_path[rel].line == 0, by_path[rel]


def test_main_tree_fails_when_a_tracked_file_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """入口收口：全量模式下有文件读不到时不得打印「未发现编号引用」。"""
    from scripts import forbidden_refs

    repo = _repo_with_unreadable_text_files(tmp_path)
    monkeypatch.chdir(repo)
    assert forbidden_refs.main(["--tree"]) == 1
    captured = capsys.readouterr()
    assert "未发现编号引用" not in captured.out, "读不到不得伪装成通过"
    assert "读取失败" in captured.out, "必须点名读不到的文件"

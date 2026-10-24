"""wheel 冒烟的真实目录泄漏断言：前后快照对比（不跑完整冒烟即可验证）。

为什么单独测这两个函数：原实现断言「真实用户缓存目录不存在」，在任何用过应用的机器上
必然失败——只有干净 runner 才碰巧成立。改成快照对比后，「本次有没有写」与「机器上是否
早有历史记录」解耦，这组用例守住三个方向：新增、改动、删除都要能判出来。
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts.wheel_smoke import SmokeFailure, assert_tree_unchanged, snapshot_tree


def test_missing_directory_snapshots_empty(tmp_path: Path) -> None:
    assert snapshot_tree(tmp_path / "nope") == {}


def test_unchanged_tree_passes(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    (root / "run-1").mkdir(parents=True)
    (root / "run-1" / "report.json").write_text("{}", encoding="utf-8")

    before = snapshot_tree(root)
    assert before  # 非空快照（含目录与文件）
    assert_tree_unchanged(root, before, why="测试")  # 不抛即通过


def test_new_entry_is_a_leak(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    before = snapshot_tree(root)

    (root / "run-2").mkdir()
    (root / "run-2" / "report.json").write_text("{}", encoding="utf-8")

    with pytest.raises(SmokeFailure, match="run-2"):
        assert_tree_unchanged(root, before, why="真实目录被写入")


def test_in_place_modification_is_a_leak(tmp_path: Path) -> None:
    """已存在的运行目录里被追加写：只看顶层条目集合会漏判，指纹必须抓住。"""
    root = tmp_path / "runs"
    (root / "run-1").mkdir(parents=True)
    target = root / "run-1" / "progress.jsonl"
    target.write_text("a\n", encoding="utf-8")
    before = snapshot_tree(root)

    target.write_text("a\nb\n", encoding="utf-8")

    with pytest.raises(SmokeFailure, match="progress.jsonl"):
        assert_tree_unchanged(root, before, why="真实目录被改写")


def test_removed_entry_is_a_leak(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    (root / "run-1").mkdir(parents=True)
    before = snapshot_tree(root)

    (root / "run-1").rmdir()

    with pytest.raises(SmokeFailure, match="run-1"):
        assert_tree_unchanged(root, before, why="真实目录被删除")

class TestScenePreservation:
    """入口级现场留存：失败路径都不得删掉诊断目录。

    旧实现只在 `except SmokeFailure` 分支置 failed 标志，`TimeoutExpired` 等意外异常
    照样走到删除分支——报错信息让人去查现场，而现场已经没了。这里在早期步骤打桩抛
    出意外异常 / 显式失败，断言退出码非零且目录仍在。
    """

    def _stub_main(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, boom: BaseException):
        from scripts import wheel_smoke

        scene = tmp_path / "scene"
        scene.mkdir()
        monkeypatch.setattr(wheel_smoke.tempfile, "mkdtemp", lambda **_kw: str(scene))

        def _raise(_out_dir: Path) -> Path:
            raise boom

        monkeypatch.setattr(wheel_smoke, "build_wheel", _raise)
        monkeypatch.setattr(wheel_smoke.sys, "argv", ["wheel_smoke.py"])
        return wheel_smoke, scene

    def test_unexpected_exception_keeps_scene(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        wheel_smoke, scene = self._stub_main(
            monkeypatch, tmp_path, TimeoutError("构建超时（模拟意外异常）")
        )
        # 新语义：非 SmokeFailure/SmokeCommandError 一律按原类型传播（traceback 留给
        # 调用方），不再转写成返回 1——旧断言 == 1 会把「吞掉异常」当成正确行为。
        with pytest.raises(TimeoutError):
            wheel_smoke.main()
        assert scene.exists(), "意外异常同样要保留现场（旧实现会删掉）"
        captured = capsys.readouterr()
        assert "现场保留" in captured.err
        assert "冒烟通过" not in captured.out + captured.err, "失败路径不得出现通过文案"

    @pytest.mark.parametrize(
        "boom",
        [
            ValueError("意外值错误"),
            TypeError("意外类型错误"),
            AttributeError("意外属性错误"),
            KeyError("意外键缺失"),
            RuntimeError("意外运行时错误"),
            KeyboardInterrupt(),
        ],
        ids=["value", "type", "attribute", "key", "runtime", "interrupt"],
    )
    def test_any_unexpected_exception_propagates_and_keeps_scene(
        self,
        boom: BaseException,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """白名单外类型（含脚本自身 bug 与 Ctrl-C）：传播 + 保留现场 + 无通过文案。"""
        wheel_smoke, scene = self._stub_main(monkeypatch, tmp_path, boom)
        with pytest.raises(type(boom)):
            wheel_smoke.main()
        assert scene.exists(), f"{type(boom).__name__} 传播时现场必须保留"
        captured = capsys.readouterr()
        assert "现场保留" in captured.err
        assert "冒烟通过" not in captured.out + captured.err

    def test_smoke_failure_keeps_scene(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from scripts.wheel_smoke import SmokeFailure

        wheel_smoke, scene = self._stub_main(
            monkeypatch, tmp_path, SmokeFailure("清单断言失败")
        )
        assert wheel_smoke.main() == 1
        assert scene.exists()
        assert "清单断言失败" in capsys.readouterr().err


class _SuccessApp:
    """start_app 的替身：成功路径只需要 log_path（wait_ready 的日志入口）。"""

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path


class _SuccessCommands:
    """成功路径的命令容器替身：close_all 可编程，并记录调用次序。

    main 里 close_all 有两次调用——try 内确认所属进程退出（第一次）与 finally 的收尾
    清理（第二次）。失败发生在哪一次决定控制流走向：第一次失败会被转写成「所属进程
    未全部确认退出」的 SmokeFailure，第二次才落到「成功路径收尾清理失败」的转失败逻辑。
    用例必须钉住抛错发生在第二次，否则断言打在了另一条分支上。
    """

    def __init__(self, on_close: Callable[[int], None]) -> None:
        self._on_close = on_close
        self.close_calls = 0

    def start_app(self, cmd: list[str], *, env: dict[str, str], cwd: Path) -> _SuccessApp:
        return _SuccessApp(Path("fake-app.log"))

    def close_all(self, timeout: float = 10.0) -> None:
        self.close_calls += 1
        self._on_close(self.close_calls)


class TestSuccessPathExitCode:
    """成功路径的收尾失败必须以非零退出码收口。

    旧实现把 `return 0` 放在 try 内：返回值在 finally 之前就已压栈，finally 里置位的
    primary_error 再也改不动它——收尾清理或现场删除失败照样以 0 退出，失败被静默。
    这里在全部检查步骤打桩通过的前提下只让收尾阶段失败，断言退出码非零。
    """

    def _stub_success_main(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, on_close: Callable[[int], None]
    ) -> tuple[Any, Path, _SuccessCommands]:
        from scripts import wheel_smoke

        scene = tmp_path / "scene"
        scene.mkdir()
        monkeypatch.setattr(wheel_smoke.tempfile, "mkdtemp", lambda **_kw: str(scene))
        commands = _SuccessCommands(on_close)
        monkeypatch.setattr(wheel_smoke, "SmokeCommands", lambda _root: commands)
        monkeypatch.setattr(wheel_smoke.sys, "argv", ["wheel_smoke.py"])

        def _noop(*_args: Any, **_kwargs: Any) -> None:
            return None

        # 各检查步骤置为 no-op：本组用例只关心收尾阶段的失败如何影响退出码。
        # console_script 也必须打桩——真实实现要求 venv 内已有可执行文件。
        for name in (
            "build_wheel",
            "assert_manifest",
            "make_venv",
            "install_wheel",
            "venv_python",
            # master 合入的 vendor 消费者断言：成功路径桩里必须一并 no-op，
            # 否则真实断言会在桩 venv 里执行并让本组用例失败在错误步骤上
            "assert_vendor_importable",
            "console_script",
            "free_port",
            "app_env",
            "wait_ready",
            "assert_http_surface",
            "assert_writes",
            "assert_no_leaks",
        ):
            monkeypatch.setattr(wheel_smoke, name, _noop)
        # 真实缓存目录探针的返回值会被 .strip()，必须给字符串；快照为空即可
        # （判泄漏的步骤已 no-op，快照内容不参与本组断言）。
        monkeypatch.setattr(wheel_smoke, "run", lambda *_args, **_kwargs: "")
        monkeypatch.setattr(wheel_smoke, "snapshot_tree", lambda _root: {})
        return wheel_smoke, scene, commands

    def test_cleanup_failure_after_success_returns_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """检查全绿但现场删除失败：必须非零退出（旧实现返回 0）。"""
        wheel_smoke, scene, commands = self._stub_success_main(
            monkeypatch, tmp_path, lambda _index: None
        )

        def _rmtree_boom(_path: object) -> None:
            raise PermissionError("现场被占用（模拟）")

        monkeypatch.setattr(wheel_smoke.shutil, "rmtree", _rmtree_boom)
        assert wheel_smoke.main() == 1, "现场删除失败必须以非零退出码收口"
        assert commands.close_calls == 2, "收尾清理必须已执行"
        assert scene.exists(), "失败路径保留现场"
        captured = capsys.readouterr()
        # 删除失败在 finally 的现场处置分支里报出（早于该分支的失败打印已执行完，
        # 所以这里只有这一条文案）；退出码由末尾的统一出口决定。
        assert "清理现场失败" in captured.err
        # 辅助断言：旧实现同样不会打印通过文案（删除失败走残余目录分支），
        # 所以它不是判别点——判别力全在 main() == 1 上。
        assert "冒烟通过" not in captured.out + captured.err

    def test_second_close_all_failure_after_success_returns_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """try 内的确认通过、finally 的收尾清理失败：必须非零退出（旧实现返回 0）。"""
        from scripts.wheel_smoke import SmokeCommandError

        raised_at: list[int] = []

        def _on_close(index: int) -> None:
            if index == 2:
                raised_at.append(index)
                raise SmokeCommandError("收尾清理失败（模拟）")

        wheel_smoke, scene, commands = self._stub_success_main(monkeypatch, tmp_path, _on_close)
        assert wheel_smoke.main() == 1, "收尾清理失败必须以非零退出码收口"
        assert raised_at == [2], "抛错必须发生在 finally 阶段的第二次调用（第一次在 try 内）"
        assert commands.close_calls == 2
        assert scene.exists()
        captured = capsys.readouterr()
        assert "冒烟收尾清理失败" in captured.err
        assert "冒烟通过" not in captured.out + captured.err


#: venv 解释器哨兵：成功路径装置把 venv_python 一并 no-op 成返回 None，
#: 「实参是 venv 里的解释器」必须换成可辨识的返回值才判得出来（回归见本节）。
_VENV_PY_SENTINEL = Path("venv-python-sentinel")


class TestVendorAssertCallSite:
    """vendor 消费者断言（assert_vendor_importable）调用点的入口级契约。

    成功路径装置把它整体 no-op（否则真实断言会跑在桩 venv 里），于是调用点
    本身没有守卫：删掉调用行、挪到应用启动之后、传成宿主解释器，既有用例全绿。
    三类静默失败各有实害——调用点消失后 vendor 装坏只能靠应用启动间接暴露；
    挪到启动之后，坏包先撞上 wait_ready 超时，报错指向「应用未就绪 + 日志尾部」
    而非「vendor 坏了」，把人引向错方向；传宿主解释器时导入落在源码树，
    检查恒通过等于没查。

    做法：复用成功路径装置，只在被测契约的四个锚点局部重打桩为记录桩，
    装置与既有用例零改动（不继承 TestSuccessPathExitCode——那会把父类两个
    用例重复收集一遍）。
    """

    def _stub_with_recorder(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> tuple[Any, Path, "_SuccessCommands", list[tuple[str, tuple[Any, ...]]]]:
        wheel_smoke_obj, scene, commands = TestSuccessPathExitCode()._stub_success_main(
            monkeypatch, tmp_path, lambda _index: None
        )
        calls: list[tuple[str, tuple[Any, ...]]] = []

        def _record(name: str, result: Any = None) -> Callable[..., Any]:
            def _stub(*args: Any, **_kwargs: Any) -> Any:
                calls.append((name, args))
                return result

            return _stub

        # console_script 在此兼作「应用启动之前」的顺序锚点：它就在
        # commands.start_app 的实参里被调用，vendor 检查挪到其后必然越界。
        monkeypatch.setattr(wheel_smoke_obj, "install_wheel", _record("install_wheel"))
        monkeypatch.setattr(
            wheel_smoke_obj, "venv_python", _record("venv_python", _VENV_PY_SENTINEL)
        )
        monkeypatch.setattr(
            wheel_smoke_obj, "assert_vendor_importable", _record("assert_vendor_importable")
        )
        monkeypatch.setattr(wheel_smoke_obj, "console_script", _record("console_script"))
        return wheel_smoke_obj, scene, commands, calls

    def test_called_once_with_venv_python_between_install_and_launch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        wheel_smoke, scene, _commands, calls = self._stub_with_recorder(monkeypatch, tmp_path)
        assert wheel_smoke.main() == 0, "记录桩不得改变成功路径的退出语义"
        captured = capsys.readouterr()
        assert "冒烟通过" in captured.out + captured.err, "必须走在成功路径上"

        vendor_calls = [args for name, args in calls if name == "assert_vendor_importable"]
        assert len(vendor_calls) == 1, "vendor 消费者断言必须恰好调用一次（删掉或复制调用点都应在此变红）"
        python_arg, cwd_arg = vendor_calls[0]
        assert python_arg is _VENV_PY_SENTINEL, (
            "必须拿 venv 里的解释器——传宿主 sys.executable 时导入落在源码树，检查恒过等于没查"
        )
        assert cwd_arg == scene, (
            "cwd 必须是本次冒烟的临时根——仓库根会把仓库树带进 sys.path，同样检不到装的包"
        )

        names = [name for name, _args in calls]
        assert names.index("install_wheel") < names.index("assert_vendor_importable"), (
            "vendor 检查必须在装包之后——wheel 安装完成之前 venv 里没有 vendor 包可导"
        )
        assert names.index("assert_vendor_importable") < names.index("console_script"), (
            "vendor 检查必须在应用启动之前——挪到启动后坏包的报错会变成「应用未就绪」，指向错误方向"
        )

    def test_vendor_failure_stops_before_app_start(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """失败即收口：vendor 断言失败必须非零退出、保留现场、绝不启动应用。

        比顺序索引更硬的「启动前」——若未来有人把检查挪进别的函数，只要失败
        仍在启动前收口，本用例仍然成立。
        """
        wheel_smoke, scene, commands, _calls = self._stub_with_recorder(monkeypatch, tmp_path)
        started: list[list[str]] = []

        def _start(cmd: list[str], *, env: dict[str, str], cwd: Path) -> _SuccessApp:
            started.append(cmd)
            return _SuccessApp(Path("fake-app.log"))

        monkeypatch.setattr(commands, "start_app", _start)

        def _boom(_python: Path, _cwd: Path) -> None:
            raise SmokeFailure("vendor 消费者断言未通过（模拟）")

        monkeypatch.setattr(wheel_smoke, "assert_vendor_importable", _boom)
        assert wheel_smoke.main() == 1, "vendor 断言失败必须以非零退出码收口"
        assert started == [], "vendor 检查失败必须在启动应用之前收口"
        assert scene.exists(), "失败路径保留现场"
        assert "vendor 消费者断言未通过" in capsys.readouterr().err

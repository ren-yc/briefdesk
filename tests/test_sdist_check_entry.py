"""sdist_check 入口行为回归：显式状态机、现场留存与清理语义。

不真构建：构建/断言函数被 stub 替换，只验证 main() 在各结果分支下的退出码、
现场目录是否保留/删除与提示输出——这些是门禁语义，不能靠真实构建的偶发失败覆盖。
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import sdist_check
from scripts.sdist_check import CheckFailure


@pytest.fixture()
def fixed_workdir(tmp_path: Path):
    """把 mkdtemp 指到已知目录，断言现场到底删没删。"""
    workdir = tmp_path / "site"
    workdir.mkdir()
    with patch.object(sdist_check, "tempfile") as fake_tf:
        fake_tf.mkdtemp.return_value = str(workdir)
        yield workdir


def _stub_sdist(out_dir):
    return Path("briefdesk-0.1.0.tar.gz")


def _stub_pass(*_args, **_kwargs):
    return []


def test_main_success_cleans_workdir_and_returns_zero(fixed_workdir: Path) -> None:
    """全部检查成功且清理成功才返回 0，现场目录被删除。"""
    with (
        patch.object(sdist_check, "build_sdist", _stub_sdist),
        patch.object(sdist_check, "assert_sdist_contents", _stub_pass),
        patch.object(sdist_check, "assert_wheel_consistency", _stub_pass),
    ):
        assert sdist_check.main() == 0
    assert not fixed_workdir.exists()


def test_main_check_failure_keeps_workdir(fixed_workdir: Path) -> None:
    """检查失败返回 1 且保留现场——回归点：以前 sys.exc_info 在 finally 已为 None，失败现场被删。"""
    def _fail(*_args, **_kwargs):
        raise CheckFailure("成员集合不一致")

    with (
        patch.object(sdist_check, "build_sdist", _fail),
        patch.object(sdist_check, "assert_sdist_contents", _stub_pass),
        patch.object(sdist_check, "assert_wheel_consistency", _stub_pass),
    ):
        assert sdist_check.main() == 1
    assert fixed_workdir.exists(), "失败必须保留现场"


@pytest.mark.parametrize("boom", [RuntimeError("构建器炸了"), KeyboardInterrupt()], ids=["error", "interrupt"])
def test_main_unexpected_exception_propagates_and_keeps_workdir(
    fixed_workdir: Path, boom: BaseException, capsys: pytest.CaptureFixture
) -> None:
    """意外异常/中断：打印现场路径后按原类型传播，现场保留。

    旧实现打印完就落回 return 1——吞掉异常后排查者按堆栈找线索却找不到堆栈，
    也分不清是「检查失败」还是「脚本自身的 bug」，Ctrl-C 同样失去中断语义。
    """
    def _raise(*_args, **_kwargs):
        raise boom

    with (
        patch.object(sdist_check, "build_sdist", _raise),
        patch.object(sdist_check, "assert_sdist_contents", _stub_pass),
        patch.object(sdist_check, "assert_wheel_consistency", _stub_pass),
        pytest.raises(type(boom)),
    ):
        sdist_check.main()
    assert fixed_workdir.exists(), "传播前必须保留现场"
    captured = capsys.readouterr()
    assert "现场保留" in captured.err
    assert str(fixed_workdir) in captured.err, "提示必须指向实际现场路径"


def test_main_cleanup_failure_returns_nonzero(fixed_workdir: Path) -> None:
    """检查通过但清理失败（Windows 文件占用）：不得静默放过，返回非零并提示残余目录。

    build_sdist 必须返回 Path（不是 _stub_pass 的空列表）：log() 会取 .name，
    桩给错类型会让「检查」阶段就先炸——意外异常被吞掉的旧实现会把这条错误
    伪装成本用例预期的 return 1，于是清理分支从未被真正执行过。
    """
    with (
        patch.object(sdist_check, "build_sdist", _stub_sdist),
        patch.object(sdist_check, "assert_sdist_contents", _stub_pass),
        patch.object(sdist_check, "assert_wheel_consistency", _stub_pass),
        patch.object(sdist_check.shutil, "rmtree", side_effect=OSError("文件被占用")),
    ):
        assert sdist_check.main() == 1
    assert fixed_workdir.exists(), "清理失败的残余目录必须可见"
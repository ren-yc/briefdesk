"""sdist_check 入口行为回归：显式状态机、现场留存与清理语义。

不真构建：构建/断言函数被 stub 替换，只验证 main() 在各结果分支下的退出码、
现场目录是否保留/删除与提示输出——这些是门禁语义，不能靠真实构建的偶发失败覆盖。
"""

from pathlib import Path
import tempfile
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

def _stub_pair(*_args, **_kwargs):
    # master 为 --keep-artifacts 把一致性检查的返回改成 (from_sdist, direct) 二元组；
    # 旧桩返回 [] 在解包处会炸成 ValueError，被 except BaseException 当意外异常传播
    return Path("briefdesk-0.1.0.tar.gz"), Path("briefdesk-0.1.0-py3-none-any.whl")


def test_main_success_cleans_workdir_and_returns_zero(fixed_workdir: Path) -> None:
    """全部检查成功且清理成功才返回 0，现场目录被删除。"""
    with (
        patch.object(sdist_check, "build_sdist", _stub_sdist),
        patch.object(sdist_check, "assert_sdist_contents", _stub_pass),
        patch.object(sdist_check, "assert_wheel_consistency", _stub_pair),
    ):
        assert sdist_check.main([]) == 0
    assert not fixed_workdir.exists()


def test_main_check_failure_keeps_workdir(fixed_workdir: Path) -> None:
    """检查失败返回 1 且保留现场——回归点：以前 sys.exc_info 在 finally 已为 None，失败现场被删。"""
    def _fail(*_args, **_kwargs):
        raise CheckFailure("成员集合不一致")

    with (
        patch.object(sdist_check, "build_sdist", _fail),
        patch.object(sdist_check, "assert_sdist_contents", _stub_pass),
        patch.object(sdist_check, "assert_wheel_consistency", _stub_pair),
    ):
        assert sdist_check.main([]) == 1
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
        patch.object(sdist_check, "assert_wheel_consistency", _stub_pair),
        pytest.raises(type(boom)),
    ):
        sdist_check.main([])
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
        patch.object(sdist_check, "assert_wheel_consistency", _stub_pair),
        patch.object(sdist_check.shutil, "rmtree", side_effect=OSError("文件被占用")),
    ):
        assert sdist_check.main([]) == 1
    assert fixed_workdir.exists(), "清理失败的残余目录必须可见"


def _real_artifact(tmp_path: Path, name: str, marker: str) -> Path:
    """真文件桩：_keep_artifacts 会 copy2，悬空 Path 会 FileNotFoundError 炸成
    意外异常——本文件 26-36 行的教训在产物保留这条路上同样成立。"""
    path = tmp_path / name
    path.write_text(marker, encoding="utf-8")
    return path


def test_keep_artifacts_copies_and_clears_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """产物保留：清掉 dist/ 旧产物、写入两件新产物；仓库真实 dist/ 不得被触碰。

    DIST_DIR 是模块级常量指向仓库目录——测试必须把它指到 tmp_path，否则会在
    开发机 dist/ 里写文件；同时断言仓库真实 dist/ 未被创建，让「实现改成默认
    参数捕获导致 monkeypatch 脱钩」立即显形而不是悄悄写穿。
    """
    dist = tmp_path / "dist"
    dist.mkdir()
    stale = _real_artifact(dist, "briefdesk-0.0.0-py3-none-any.whl", "stale")
    sdist = _real_artifact(tmp_path, "briefdesk-0.1.0.tar.gz", "sdist-marker")
    wheel = _real_artifact(tmp_path, "briefdesk-0.1.0-py3-none-any.whl", "wheel-marker")
    monkeypatch.setattr(sdist_check, "DIST_DIR", dist)
    repo_dist = Path(sdist_check.__file__).parent.parent / "dist"
    existed_before = repo_dist.exists()

    sdist_check._keep_artifacts(sdist, wheel)

    assert not stale.exists(), "历史产物必须被清掉（通配命中会假绿或假红）"
    assert (dist / sdist.name).read_text(encoding="utf-8") == "sdist-marker"
    assert (dist / wheel.name).read_text(encoding="utf-8") == "wheel-marker"
    if not existed_before:
        assert not repo_dist.exists(), "测试不得在仓库目录创建 dist/"


def test_main_keep_artifacts_copy_failure_is_check_failure(
    fixed_workdir: Path, capsys: pytest.CaptureFixture
) -> None:
    """--keep-artifacts 中途 OSError（磁盘满/权限）：按「检查失败」收场而非意外终止。

    旧实现让 OSError 走 except BaseException：dist/ 已清旧产物、可能留半份新
    产物，退出语义还是带 traceback 的「意外终止」——排查者分不清是发布环境
    故障还是检查真的失败。转成 CheckFailure 后与其它检查失败同一出口。
    """
    # 桩必须返回真实存在的文件：copy2 需要真实源（悬空 Path 会先炸 FileNotFoundError，
    # 走不到要测的 OSError 分支）
    with tempfile.TemporaryDirectory() as d:
        sdist = _real_artifact(Path(d), "briefdesk-0.1.0.tar.gz", "sdist-marker")
        wheel = _real_artifact(Path(d), "briefdesk-0.1.0-py3-none-any.whl", "wheel-marker")

        def _stub_pair_real(*_args, **_kwargs):
            return sdist, wheel

        def _boom_copy(_src, _dst):
            raise OSError("磁盘空间不足（模拟）")

        with (
            patch.object(sdist_check, "build_sdist", _stub_sdist),
            patch.object(sdist_check, "assert_sdist_contents", _stub_pass),
            patch.object(sdist_check, "assert_wheel_consistency", _stub_pair_real),
            patch.object(sdist_check, "DIST_DIR", Path(d) / "dist"),
            patch.object(sdist_check.shutil, "copy2", _boom_copy),
        ):
            assert sdist_check.main(["--keep-artifacts"]) == 1
    captured = capsys.readouterr()
    assert "失败" in captured.err, "必须按检查失败语义报错"
    assert "意外终止" not in captured.err, "不得落入意外异常分支（traceback 收场）"
    assert fixed_workdir.exists(), "检查失败必须保留现场"

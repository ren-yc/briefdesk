"""重标定脚本纯函数单测（不触 DB / 不触网络）。

recalibrate() 与 _percentile() 是 scripts/recalibrate_similarity.py 的纯
计算核心（impl 口径「纯函数可单测」）——构造已知分数分布断言分位数、
召回率与触发率，防公式回归（该脚本的输出是阈值决策依据，算错分位数会把
重标定引向错误取值）。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "recalibrate_similarity", _REPO / "scripts" / "recalibrate_similarity.py"
)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover — 文件必在
    raise RuntimeError("无法加载 recalibrate_similarity.py")
_mod = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("recalibrate_similarity", _mod)
_SPEC.loader.exec_module(_mod)


class TestPercentile:
    def test_single_value(self):
        assert _mod._percentile([0.5], 50) == 0.5

    def test_interpolation_midpoint(self):
        # 两点中位 = 线性内插中点
        assert _mod._percentile([0.0, 1.0], 50) == 0.5

    def test_extremes(self):
        vals = [0.1, 0.2, 0.3]
        assert _mod._percentile(vals, 0) == 0.1
        assert _mod._percentile(vals, 100) == 0.3


class TestRecalibrate:
    def test_known_distribution(self):
        # 3 个正样本分数 [0.9, 0.8, 0.5]，2 个负样本 [0.2, 0.1]
        pairs = [
            ("aa", "aa", True),
            ("ab", "ac", True),
            ("ad", "ae", True),
            ("bb", "bc", False),
            ("bd", "be", False),
        ]
        # 直接注入分数不可行（recalibrate 现算），改用可控标题对：
        # "aa" vs "aa" 同文 = 1.0；其余对差异大 → 分数低。断言结构性性质。
        result = _mod.recalibrate(pairs, [0.3, 0.5])
        assert result["pairs"] == 5
        assert result["positive_pairs"] == 3
        # 阈值 0.3：正样本至少命中同文那条 → 召回 ≈ 1/3；触发数 ≥ 命中数。
        # 容忍 1e-4：结果经 round(x, 4) 截位，0.3333 严格小于理论值 1/3，
        # 直接 >= 比较会因舍入方向假红（本文件曾因类名不被 pytest 收集而
        # 从未运行过，这类断言误差只有收集修复后才暴露）
        t03 = next(t for t in result["thresholds"] if t["threshold"] == 0.3)
        assert t03["positive_recall"] is not None
        assert t03["positive_recall"] == pytest.approx(1 / 3, abs=1e-4)
        assert t03["triggered"] >= 1
        # 阈值升高触发数单调不增
        t05 = next(t for t in result["thresholds"] if t["threshold"] == 0.5)
        assert t05["triggered"] <= t03["triggered"]
        assert t05["positive_recall"] <= t03["positive_recall"]

    def test_empty_positive_returns_none_recall(self):
        pairs = [("aa", "ab", False)]
        result = _mod.recalibrate(pairs, [0.3])
        assert result["positive_pairs"] == 0
        assert result["thresholds"][0]["positive_recall"] is None

    def test_empty_pairs(self):
        result = _mod.recalibrate([], [0.3])
        assert result["pairs"] == 0
        assert result["thresholds"][0]["triggered"] == 0

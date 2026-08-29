"""前端关键词清单工厂回归测试（通过 Node vm 执行 ui_keyword_list_test.mjs）。

背景：关键词订阅与降噪黑名单本是同一套东西（localStorage 存
[{id, keywords, enabled}]、同款 .subs-row 渲染、添加/启停/删除三组事件、
“启用组空格分词 OR 命中”判定），此前各写一份共 8 函数 6 处理器。现统一由
createKeywordList 产出实例，差异收进选项。本测试守两处易被抹平的细节：

* 两侧唯一的命中差异是 fields——黑名单额外读 sender_name（按发送人降噪），
  订阅不读（按发送人订阅无意义）。
* items 引用全程稳定（删除/载入都原地改），否则实例返回的 items 与内部
  状态脱钩，updateSubsBadge 会读到删除前的旧数组。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "ui_keyword_list_test.mjs"
_ROOT = Path(__file__).resolve().parents[1]
_NODE = shutil.which("node")


@unittest.skipUnless(_NODE, "Node.js not available; skipping frontend vm regression test")
class UiKeywordListFactoryTest(unittest.TestCase):
    def test_subscription_and_blocklist_share_one_factory(self):
        result = subprocess.run(
            [_NODE, str(_SCRIPT)],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

"""benchmark 插件专属配置 — 从 .env 读取 BENCHMARK_* 环境变量。

与 app 级配置（briefdesk/config.py）分离：只有启用 benchmark 插件时才被加载。
核心 config.py 的口径是「插件前缀归插件所有，不在核心声明」，故 BENCHMARK_
前缀的字段全部落在这里，设置页经 BenchmarkPlugin.settings_schema() 自动展示。

本插件无密钥型字段，故不定义 KEYRING_FIELDS（基类对此是可选的）。
"""

from typing import ClassVar

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from briefdesk.settings_base import KeyringSettingsBase


class BenchmarkSettings(KeyringSettingsBase):
    """基准插件配置（每次进入基准环境时重新实例化，设置页改动即时生效）。

    drain_stall_seconds 是**无进展**阈值而非总时长上限：进展信号
    （pendingCount / activeBatches）都是批粒度的，只在批边界变化。取 720s
    的依据是覆盖「单批中两段串行 AI 调用在常规退化下的耗时」——分类
    （最坏 120s × 3 次尝试 = 360s）与其后的「时间提取 ∥ 标题概括」并行段
    （再 360s）。**不应低于单请求最坏耗时**（120s × 3 = 360s），否则正常
    慢批会被误中止。

    未覆盖的场景（这些情况下仍可能误中止，需要调大本值）：入库阶段逐条
    判官（每次最坏约 90s，按批内条数叠加）、OCR 与嵌入请求、分类与时间
    提取两处的拆半递归。

    run_stall_seconds 是**子进程运行期**的无进展阈值，与上面那个不是一回事：
    它的进展信号是 progress.jsonl 的行数增长（**用例粒度**），阈值必须 ≥
    单个用例的最坏耗时 × 进度节流间隔。默认 600s 是按「分类单次 120s × 3 次
    重试 = 360s」再留约 1.6 倍余量定的；父进程还会按 --progress-every 线性放大。
    """

    drain_stall_seconds: int = Field(default=720, ge=1)  # env: BENCHMARK_DRAIN_STALL_SECONDS
    run_mode: str = Field(default="inproc")  # env: BENCHMARK_RUN_MODE（inproc|subprocess）
    pause_pipeline: bool = Field(default=True)  # env: BENCHMARK_PAUSE_PIPELINE
    keep_runs: int = Field(default=5, ge=1)  # env: BENCHMARK_KEEP_RUNS
    run_timeout_seconds: int = Field(default=0, ge=0)  # env: BENCHMARK_RUN_TIMEOUT_SECONDS
    run_stall_seconds: int = Field(default=600, ge=1)  # env: BENCHMARK_RUN_STALL_SECONDS

    # ClassVar 注解声明类级配置而非字段（RUF012）
    model_config: ClassVar[SettingsConfigDict] = {"env_prefix": "BENCHMARK_"}

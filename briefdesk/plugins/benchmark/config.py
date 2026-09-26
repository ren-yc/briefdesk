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

    run_stall_seconds 是**运行期**的无进展阈值：进展信号是 progress.jsonl 的行数
    增长（**用例粒度**），阈值必须 ≥ 单个用例的最坏耗时 × 进度节流间隔。默认
    600s 是按「分类单次 120s × 3 次重试 = 360s」再留约 1.6 倍余量定的；父进程
    还会按 --progress-every 线性放大。
    """

    pause_pipeline: bool = Field(default=True)  # env: BENCHMARK_PAUSE_PIPELINE
    keep_runs: int = Field(default=5, ge=1)  # env: BENCHMARK_KEEP_RUNS
    run_timeout_seconds: int = Field(default=0, ge=0)  # env: BENCHMARK_RUN_TIMEOUT_SECONDS
    run_stall_seconds: int = Field(default=600, ge=1)  # env: BENCHMARK_RUN_STALL_SECONDS

    # ClassVar 注解声明类级配置而非字段（RUF012）
    model_config: ClassVar[SettingsConfigDict] = {"env_prefix": "BENCHMARK_"}

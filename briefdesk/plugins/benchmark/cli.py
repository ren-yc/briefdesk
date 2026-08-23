"""LLM 功能基准测试 CLI（benchmark 插件命令行入口）。

用法（在仓库根目录执行）：
    python -m briefdesk.plugins.benchmark.cli --dry-run
    python -m briefdesk.plugins.benchmark.cli --feature classify
    python -m briefdesk.plugins.benchmark.cli --charts

基准运行会真实调用 AI（读 .env 的 AI_API_KEY/AI_API_BASE/AI_MODEL），
临时库经补丁隔离（不动应用库连接）。文件数据集在 cases/ 目录
（<feature>.json 优先，其次网页导出的 <feature>.fromweb.json，示例为
<feature>.example.json）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path

from briefdesk.config import config
from briefdesk.logger import fmt_dur
from briefdesk.plugins.benchmark import engine as bench_engine
from briefdesk.plugins.benchmark.html_report import build_html_report, save_html_report
from briefdesk.plugins.benchmark.report import render_feature_block, save_json_report
from briefdesk.plugins.benchmark.schema import (
    FEATURES,
    BaseCase,
    CategoryDef,
    DatasetError,
)
from briefdesk.plugins.benchmark.store import FROMWEB_SUFFIX

logger = logging.getLogger(__name__)

DEFAULT_CASES_DIR = Path(__file__).resolve().parent / "cases"
DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "reports"


# ── 数据集解析 ──


def _resolve_dataset(feature: str, cases_dir: Path, dataset: str | None) -> Path:
    if dataset:
        return Path(dataset)
    primary = cases_dir / f"{feature}.json"
    if primary.exists():
        return primary
    fromweb = cases_dir / f"{feature}{FROMWEB_SUFFIX}"
    if fromweb.exists():
        print(f"  提示：未找到 {primary.name}，使用网页导出的用例文件 {fromweb.name}")
        return fromweb
    fallback = cases_dir / f"{feature}.example.json"
    if fallback.exists():
        print(f"  提示：未找到 {primary.name}，使用示例数据集 {fallback.name}")
        return fallback
    raise FileNotFoundError(
        f"未找到测试集 {primary}（可从 {fallback.name} 复制后手动编辑添加数据，"
        f"或在网页设置里「导出当前列表为基准用例」生成 {fromweb.name}）"
    )


def _load_file_features(
    features: list[str], cases_dir: Path, dataset: str | None
) -> dict[str, tuple[list[BaseCase], list[CategoryDef] | None, Path]]:
    """返回 {feature: (cases, categories, path)}；categories 来自 classify 数据集声明。"""
    out: dict[str, tuple[list[BaseCase], list[CategoryDef] | None, Path]] = {}
    for f in features:
        path = _resolve_dataset(f, cases_dir, dataset)
        ds = bench_engine.load_file_dataset(path)
        out[f] = (bench_engine.load_file_cases(path), ds.categories, path)
    return out


# ── dry-run（不调用 AI / 不访问 DB）──


def _dry_stats(feature: str, cases: list) -> dict:
    from briefdesk.plugins.benchmark.schema import (
        ClassifyCase,
        DedupCase,
        MergeCase,
        TitleCase,
    )

    if feature == "classify":
        cls_cases = [c for c in cases if isinstance(c, ClassifyCase)]
        messages = sum(len(c.messages) for c in cls_cases)
        expected = sum(len(c.expected) for c in cls_cases)
        cats = sorted({e.category for c in cls_cases for e in c.expected})
        return {
            "cases": len(cases),
            "messages": messages,
            "expected": expected,
            "noise": messages - expected,
            "categories": cats,
        }
    if feature == "dedup":
        dd_cases = [c for c in cases if isinstance(c, DedupCase)]
        return {
            "cases": len(cases),
            "items": sum(len(c.items) for c in dd_cases),
            "same_true": sum(1 for c in dd_cases if c.expected.same),
        }
    if feature == "merge":
        mg_cases = [c for c in cases if isinstance(c, MergeCase)]
        return {
            "cases": len(cases),
            "merge_true": sum(1 for c in mg_cases if c.expected.merge),
        }
    tt_cases = [c for c in cases if isinstance(c, TitleCase)]
    return {
        "cases": len(cases),
        "exact": sum(1 for c in tt_cases if c.expected.title),
        "keyword": sum(1 for c in tt_cases if c.expected.keywords),
    }


def run_dry_run(
    features: list[str], cases_dir: Path, dataset: str | None
) -> dict:
    """校验测试集并打印统计（纯文件解析，不调用 AI / 不访问 DB）。"""
    payload: dict = {"features": {}}

    for f in features:
        path = _resolve_dataset(f, cases_dir, dataset)
        cases = bench_engine.load_file_cases(path)
        ds = bench_engine.load_file_dataset(path)
        stats = _dry_stats(f, cases)
        payload["features"][f] = {"dataset": str(path), **stats}
        print(f"== {f}（校验通过）==")
        print(f"  数据集: {path}")
        if ds.description:
            print(f"  说明: {ds.description}")
        if f == "classify":
            print(
                f"  用例 {stats['cases']} 条，消息 {stats['messages']} 条，"
                f"期望分类 {stats['expected']} 条，闲聊/噪声 {stats['noise']} 条"
            )
            if stats["categories"]:
                print(f"  期望类别: {', '.join(stats['categories'])}")
            if ds.categories:
                print(
                    "  类别覆盖: "
                    + ", ".join(f"{c.name}（{c.prompt or '无说明'}）" for c in ds.categories)
                )
        elif f == "dedup":
            print(
                f"  用例 {stats['cases']} 条，历史卡片 {stats['items']} 条，"
                f"期望 same=true {stats['same_true']} 条"
            )
        elif f == "merge":
            print(
                f"  用例 {stats['cases']} 条，期望 merge=true {stats['merge_true']} 条"
            )
        else:
            print(
                f"  用例 {stats['cases']} 条，"
                f"精确匹配期望 {stats['exact']} 条，关键词期望 {stats['keyword']} 条"
            )
    return payload


# ── 正式运行 ──


def _progress_printer(started: float) -> Callable[[bench_engine.CaseProgress], None]:
    """构造评估进度打印器：每完成 5 条输出一行（最后一条恒输出）。

    失败数非零时标注（失败用例另有 logger WARNING 走 stderr，不混入本行）。
    """

    def _print(p: bench_engine.CaseProgress) -> None:
        if p.done % 5 == 0 or p.done == p.total:
            failed = f"（失败 {p.failed}）" if p.failed else ""
            print(
                f"  {p.feature} {p.done}/{p.total}{failed}"
                f" · {fmt_dur(time.monotonic() - started)}",
                flush=True,
            )

    return _print


async def run_benchmark(args: argparse.Namespace) -> dict:
    features = list(args.feature)
    if "all" in features:
        features = list(FEATURES)
    if args.model:
        config.ai_model = args.model
    if args.disable_thinking:
        config.ai_disable_thinking = True
    print(f"AI 模型: {config.ai_model}（并发 {max(1, args.concurrency)}）")

    loaded = _load_file_features(features, Path(args.cases_dir), args.dataset)
    cases_by_feature = {f: item[0] for f, item in loaded.items()}
    categories = loaded.get("classify", (None, None, None))[1]
    dataset_label = {f: str(item[2]) for f, item in loaded.items()}

    for f in features:
        print(f"\n>>> 运行 {f}：{len(cases_by_feature.get(f, []))} 条用例")

    payload, evals_by_feature = await bench_engine.run_benchmark_cases(
        cases_by_feature,
        categories,
        concurrency=max(1, args.concurrency),
        dataset_label=dataset_label,
        progress=_progress_printer(time.monotonic()),
    )
    for f, data in payload["features"].items():
        for line in render_feature_block(
            f, evals_by_feature.get(f, []), data["summary"], data.get("elapsed_sec")
        ):
            print(line)

    total_elapsed = payload.get("elapsed_sec")
    if total_elapsed is not None:
        print(f"\n总用时: {fmt_dur(total_elapsed)}")

    path = save_json_report(args.out, payload)
    print(f"\n结果已导出: {path}")
    if args.charts:
        html_path = save_html_report(
            args.out, payload["run_id"], build_html_report(payload)
        )
        print(f"图表报告已导出: {html_path}")
    return payload


# ── CLI ──


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m briefdesk.plugins.benchmark.cli",
        description="LLM 功能基准测试（classify / dedup / merge / title），"
        "真实调用 AI 评估输出质量。",
    )
    parser.add_argument(
        "--feature",
        nargs="*",
        choices=[*FEATURES, "all"],
        default=["all"],
        help="要运行的功能（默认 all）",
    )
    parser.add_argument(
        "--cases-dir",
        default=str(DEFAULT_CASES_DIR),
        help=f"文件数据集目录（默认 {DEFAULT_CASES_DIR}）",
    )
    parser.add_argument(
        "--dataset",
        default=None,
        help="指定单个数据集文件（此时 --feature 只能指定一个功能）",
    )
    parser.add_argument(
        "--concurrency", type=int, default=1, help="用例并发数（默认 1，串行）"
    )
    parser.add_argument(
        "--model", default=None, help="覆盖 AI_MODEL（评估不同模型时用）"
    )
    parser.add_argument(
        "--disable-thinking",
        action="store_true",
        help="等价于 AI_DISABLE_THINKING=true（Qwen 系关闭思考模式）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只校验测试集并打印统计，不调用 AI、不访问数据库",
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT_DIR),
        help=f"结果 JSON 导出目录（默认 {DEFAULT_OUT_DIR}）",
    )
    parser.add_argument(
        "--charts",
        action="store_true",
        help="额外生成自包含 HTML 图表报告（内联 SVG，无第三方依赖，浏览器直接打开）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # 终端/管道输出统一 UTF-8：避免 ✔/✘ 等字符在 GBK 管道（如重定向）下崩溃
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass  # 非可重配置流（如测试捕获）保持原样
    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
    )
    args = _parse_args(argv)
    features = list(args.feature)
    if "all" in features:
        features = list(FEATURES)
    if args.dataset and len(features) != 1:
        print("错误：--dataset 只允许与单个 --feature 一起使用", file=sys.stderr)
        return 2
    try:
        if args.dry_run:
            if args.charts:
                print("提示：dry-run 无运行结果，跳过图表报告", file=sys.stderr)
            run_dry_run(features, Path(args.cases_dir), args.dataset)
            print("\ndry-run 完成：测试集校验通过，未调用 AI。")
        else:
            asyncio.run(run_benchmark(args))
    except (DatasetError, FileNotFoundError, json.JSONDecodeError) as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

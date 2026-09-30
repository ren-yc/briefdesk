"""基准子进程入口 — 在独立进程里执行用例，父子只经文件和显式环境变量交换。

子进程使用运行目录内的 scratch 数据库，拥有独立的配置、数据库连接、去重缓存和
向量缓存；父进程不切换生产库连接，也不共享生产进程的同步状态。

产物（全部写在 run-dir 内，统一显式 utf-8）：

    meta.json       开始工作前写：run_id/features/started_at/pid/db_path/source，
                    结束时补一条终态记录（type/kind/exit_code/finished_at）
    progress.jsonl  每行一条完整 JSON：start | progress | 终态行
    report.json     与既有 payload 同构（前端与图表无需变更）
    report.html     build_html_report 产物

结果分类的唯一权威是**终态行**（`{"type":"done"|"error","kind":...}`）；退出码只承诺
0 成功 / 2 用法错误 / 其余非 0——逃逸异常、Windows 上父进程的 terminate、db.py 的
SchemaMismatch SystemExit(1) 都落在 1 上，用退出码分类必然误判。

数据库连接必须在退出前关闭：aiosqlite 的 worker 线程不是 daemon 线程，单例持有连接时
解释器退出会 join 该线程而挂死，父进程按句柄判定就会永远认为它在运行（见 briefdesk.db
顶部注释与 close_db 的 docstring）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from briefdesk import ai_ports
from briefdesk.config import config
from briefdesk.db import close_db
from briefdesk.plugin.base import (
    AIProvider,
    ChatChoice,
    ChatMessage,
    ChatResponse,
)
from briefdesk.plugins.ai_provider.engine import Provider
from briefdesk.plugins.benchmark import engine as bench_engine
from briefdesk.plugins.benchmark import providers as bench_providers
from briefdesk.plugins.benchmark.html_report import build_html_report
from briefdesk.plugins.benchmark.schema import (
    FEATURES,
    BaseCase,
    CategoryDef,
    DatasetError,
)

logger = logging.getLogger(__name__)

DEFAULT_CASES_DIR = Path(__file__).resolve().parent / "cases"

# 退出码：只有 0（成功）与 2（用法错误）是契约承诺的粒度，其余一律非 0。
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_DATASET = 1
EXIT_INTERNAL = 4

_AI_REAL = "real"
_AI_STUB = "stub"


# ── AI 端口 ──


class _StubMessage:
    # 属性类型按协议原样标注：协议的可变属性是不变的，写成 str 会与
    # ChatMessage.content（str | None）不兼容。
    content: str | None

    def __init__(self, content: str) -> None:
        self.content = content


class _StubChoice:
    message: ChatMessage
    finish_reason: str | None

    def __init__(self, content: str) -> None:
        self.message = _StubMessage(content)
        self.finish_reason = "stop"


class _StubResponse:
    def __init__(self, content: str) -> None:
        # 注解成协议的 list[ChatChoice]：choices 是可变属性，list[_StubChoice] 与之
        # 不兼容（不变性），换掉内层类型比放宽协议更省事。
        self.choices: list[ChatChoice] = [_StubChoice(content)]


class _StubProvider(AIProvider):
    """测试用 AI 替身：按提示词识别调用方功能，返回固定结果。

    只覆盖基准的四个功能；**认不出来就抛错**而不是编一个看起来有效的结果——
    静默的假结果会让基准指标看起来正常却毫无意义。
    不启用嵌入（is_embedding_enabled False），避免造假向量把判重路径带偏。
    """

    _MARKERS: tuple[tuple[str, str], ...] = (
        ('{"merge"', "merge"),
        ("信息去重助手", "dedup"),
        ("重拟一个标题", "title"),
        ('{"index"', "classify"),
    )

    async def chat(
        self,
        messages: list[dict],
        *,
        temperature: float,
        max_tokens: int,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> ChatResponse:
        del temperature, max_tokens, timeout, max_retries  # 替身不看采样参数
        system = ""
        user = ""
        for m in messages:
            if m.get("role") == "system":
                system = str(m.get("content") or "")
            elif m.get("role") == "user":
                user = str(m.get("content") or "")
        feature = next((f for marker, f in self._MARKERS if marker in system), "")
        if feature == "merge":
            return _StubResponse('{"merge": false}')
        if feature == "dedup":
            return _StubResponse('{"is_duplicate": false}')
        if feature == "title":
            return _StubResponse("stub 标题")
        if feature == "classify":
            return _StubResponse(json.dumps(await self._classify_payload(user)))
        raise RuntimeError(
            "AI 替身不认识这次请求（--ai-provider stub 只覆盖基准四个功能的提示词）；"
            "需要跑真实供应商请去掉该参数"
        )

    async def rag_chat(
        self,
        messages: list[dict],
        *,
        temperature: float,
        max_tokens: int,
        model: str = "",
        api_base: str = "",
        api_key: str = "",
    ) -> ChatResponse:
        del model, api_base, api_key  # 基准不跑 RAG；有调用就按 chat 的固定结果回
        return await self.chat(messages, temperature=temperature, max_tokens=max_tokens)

    async def _classify_payload(self, user: str) -> dict[str, Any]:
        from briefdesk.db import get_enabled_categories

        cats = await get_enabled_categories()
        if not cats:
            raise RuntimeError("AI 替身分类失败：scratch 库里没有启用的类别")
        items = _count_classify_items(user)
        data = [
            {"index": i, "include": True, "category": cats[0]["name"]}
            for i in range(items)
        ]
        return {"task": "classify", "data": data}

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("AI 替身在 stub 模式下不提供嵌入（is_embedding_enabled 为假）")

    def is_embedding_enabled(self) -> bool:
        return False

    def embed_model_name(self) -> str:
        return "stub"


def _count_classify_items(user: str) -> int:
    """数一下分类批里有多少条消息（替身要按 index 逐条回）。

    user 消息里的数据段是 JSON 数组；解析不出来就退回 0，让调用方按空批处理。
    """
    start = user.find("[")
    end = user.rfind("]")
    if start < 0 or end <= start:
        return 0
    try:
        data = json.loads(user[start : end + 1])
    except json.JSONDecodeError:
        return 0
    return len(data) if isinstance(data, list) else 0


# ── 产物写入 ──


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


class _ProgressLog:
    """progress.jsonl 追加器：整行追加 + flush，父进程只解析完整行。

    不用原子替换：Windows 上目标文件被父进程打开时 os.replace 会失败
    （PermissionError WinError 5），追加是这里唯一跨平台安全的写法。
    """

    def __init__(self, path: Path, every: int) -> None:
        self._every = max(1, every)
        self._fh = path.open("a", encoding="utf-8")

    def start(self, info: dict[str, Any]) -> None:
        self._line({"type": "start", **info})

    def progress(self, feature: str, done: int, total: int, failed: int) -> None:
        # 节流：与 CLI 打印器同口径——最后一条恒输出，否则尾部进度会丢
        if done % self._every and done != total:
            return
        self._line(
            {
                "type": "progress",
                "feature": feature,
                "done": done,
                "total": total,
                "failed": failed,
            }
        )

    def terminal(self, kind: str, **extra: Any) -> None:
        type_ = "done" if kind == "done" else "error"
        self._line({"type": type_, "kind": kind, **extra})

    def _line(self, payload: dict[str, Any]) -> None:
        self._fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


# ── 用例装载 ──


async def _load_cases(
    args: argparse.Namespace, features: list[str]
) -> tuple[dict[str, list[BaseCase]], list[CategoryDef] | None, Any]:
    """返回 (cases_by_feature, category_defs, dataset_label)。

    file 与 fromweb 走各自的装载路径：前者复用 CLI 的数据集解析（含示例回退），
    后者读 cases/<feature>.fromweb.json。两条路径都受 --cases-dir 约束，否则
    测试只能往包内真实用例目录里写夹具。
    """
    if args.source == "fromweb":
        cases = {f: await _load_web(f, args.cases_dir) for f in features}
        return cases, None, bench_engine.FROMWEB_SOURCE_LABEL

    from briefdesk.plugins.benchmark.cli import _load_file_features

    loaded = _load_file_features(features, Path(args.cases_dir), None)
    cases_by_feature = {f: item[0] for f, item in loaded.items()}
    categories = loaded.get("classify", (None, None, None))[1]
    dataset_label = {f: str(item[2]) for f, item in loaded.items()}
    return cases_by_feature, categories, dataset_label


async def _load_web(feature: str, cases_dir: str) -> list[BaseCase]:
    return await bench_engine.load_web_cases(feature, cases_dir)


# ── 主流程 ──


async def _run(args: argparse.Namespace, run_dir: Path, run_id: str) -> int:
    features = list(FEATURES) if "all" in args.features else list(args.features)
    meta_path = run_dir / "meta.json"
    meta: dict[str, Any] = {
        "run_id": run_id,
        "features": features,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pid": os.getpid(),
        "db_path": str(args.db),
        "source": args.source,
    }
    # meta 必须在开始工作前落盘：否则被杀/崩溃的运行没有 meta，会被当成残目录清掉，
    # 而「失败目录保留供排查」正需要它。
    _write_json(meta_path, meta)
    log = _ProgressLog(run_dir / "progress.jsonl", args.progress_every)
    log.start(
        {
            "run_id": run_id,
            "features": features,
            "source": args.source,
            "pid": os.getpid(),
        }
    )
    kind = "internal"
    exit_code = EXIT_INTERNAL
    detail: dict[str, Any] = {}
    try:
        # 先载入用例：数据集声明的类别要喂给建库步骤（fromweb 没有声明则为 None，
        # 由 init_schema 播种默认类别）。装载本身不碰库。
        cases_by_feature, categories, dataset_label = await _load_cases(args, features)
        total_cases = sum(len(c) for c in cases_by_feature.values())
        # 子进程自己建库：环境缝是父进程为隔离自己的连接而发明的，这里不需要
        await bench_providers.prepare_scratch(categories)
        ai_ports.set_ai(_StubProvider() if args.ai_provider == _AI_STUB else Provider())
        payload, _evals = await bench_engine.run_benchmark_cases(
            cases_by_feature,
            categories,
            concurrency=max(1, args.concurrency),
            dataset_label=dataset_label,
            progress=lambda p: log.progress(p.feature, p.done, p.total, p.failed),
            run_id=run_id,
        )
        _write_json(run_dir / "report.json", payload)
        (run_dir / "report.html").write_text(
            build_html_report(payload), encoding="utf-8"
        )
        kind = "done"
        exit_code = EXIT_OK
        detail = {
            "elapsed_sec": payload.get("elapsed_sec"),
            "cases": total_cases,
            "failed": sum(
                int(v.get("failed", 0) or 0)
                for v in payload.get("features", {}).values()
            ),
        }
        log.terminal(kind, **detail)
    except (DatasetError, FileNotFoundError, json.JSONDecodeError) as e:
        kind = "dataset"
        exit_code = EXIT_DATASET
        log.terminal(kind, message=f"{type(e).__name__}: {e}")
    except Exception as e:  # 兜底失败也要留下终态行，否则父进程只剩退出码可判
        logger.exception("基准运行失败")
        kind = "internal"
        exit_code = EXIT_INTERNAL
        log.terminal(kind, message=f"{type(e).__name__}: {e}")
    finally:
        meta["terminal"] = {
            "type": "done" if kind == "done" else "error",
            "kind": kind,
            "exit_code": exit_code,
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            **detail,
        }
        _write_json(meta_path, meta)
        log.close()
    return exit_code


def _new_run_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m briefdesk.plugins.benchmark.runner",
        description="在独立进程里跑基准（父子只经文件交换）。",
    )
    parser.add_argument("--run-dir", required=True, help="进度与报告落盘目录")
    parser.add_argument("--db", required=True, help="本次运行的 scratch 库路径")
    parser.add_argument("--run-id", default=None, help="结果标识（缺省自动生成）")
    parser.add_argument(
        "--cases-dir",
        default=str(DEFAULT_CASES_DIR),
        help=f"用例目录（默认 {DEFAULT_CASES_DIR}）",
    )
    parser.add_argument(
        "--features",
        nargs="*",
        choices=[*FEATURES, "all"],
        default=["all"],
        help="要运行的功能（默认 all）",
    )
    parser.add_argument(
        "--source", choices=["file", "fromweb"], default="file", help="用例来源"
    )
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--progress-every", type=int, default=1)
    parser.add_argument(
        "--ai-provider",
        choices=[_AI_REAL, _AI_STUB],
        default=_AI_REAL,
        help="AI 端口：real 用真实供应商，stub 用内置替身（仅测试）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
    )
    # --db 与生产库同路径 → 用法错误：兜底只保证不打开该文件以外的库，
    # 挡不住「--db 本身就是生产库」这种误用。比较必须在改写 config.db_path 之前。
    production_db = str(config.db_path)
    try:
        args = _parse_args(argv)
    except SystemExit as e:  # 进程内直调时 argparse 用异常而非返回码表达用法错误
        return EXIT_USAGE if e.code is None else int(e.code)
    if Path(args.db).resolve() == Path(production_db).resolve():
        print(f"错误：--db 指向生产库（{production_db}），拒绝运行", file=sys.stderr)
        return EXIT_USAGE

    run_dir = Path(args.run_dir)
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # 连落盘目录都建不出来，没有任何地方可以写终态行；此时退出码就是唯一出口。
        print(f"错误：无法创建 --run-dir（{run_dir}）：{e}", file=sys.stderr)
        return EXIT_INTERNAL
    run_id = args.run_id or _new_run_id()
    config.db_path = args.db
    # 关库必须与运行在**同一个事件循环**内完成：aiosqlite 连接绑定创建它的循环，
    # 另起一个循环去 close 会失败。放在 finally 里保证失败路径也会关——漏关会让
    # 解释器退出时 join 非 daemon 的 worker 线程而挂死。
    return asyncio.run(_run_and_close(args, run_dir, run_id))


async def _run_and_close(args: argparse.Namespace, run_dir: Path, run_id: str) -> int:
    try:
        return await _run(args, run_dir, run_id)
    finally:
        await close_db()


if __name__ == "__main__":
    sys.exit(main())

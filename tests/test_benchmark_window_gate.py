"""基准窗口的 HTTP 契约：写闸门、读黑名单、导出产出点守卫与静态守卫。

窗口内单例指向临时基准库，而写路由会把「内存副作用」照常施加到生产单例
（事件发布 → 去重缓存增删），备份/导出还会把临时库内容落成用户手里的文件。
故窗口期变更路由一律 409（显式白名单除外），会产出文件的三条读路由同样
409，导出产出函数上另有一道与名单无关的无条件守卫。

写闸门用例从 app.routes **动态枚举**变更路由，而不是手写清单：手写清单在
新增路由时会静默失去覆盖。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient

import briefdesk.server as srv
from briefdesk.db import db_redirect
from briefdesk.plugins.benchmark import router as bench_router
from briefdesk.server import routes_items as srv_routes
from briefdesk.server import window_guard

# 显式装配 benchmark 插件路由（模拟 main 的装配；include_plugin_router 幂等）：
# 不装配时 /api/benchmark/* 不在 app.routes 里，白名单路由会被静默跳过
srv.include_plugin_router(bench_router.router)

_ROOT = Path(__file__).resolve().parents[1]
_BRIEFDESK = _ROOT / "briefdesk"

_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_PATH_PARAM_RE = re.compile(r"\{[^}]+\}")

# 产出文件的读路由白名单：都不读数据库，故窗口期可放行
_FILELESS_READ_WHITELIST = {
    "/": "只从 ui/ 目录读 SPA 首页，不读 DB",
    "/plugin-assets/{name}/{path:path}": "只读插件 ui/ 资源目录，不读 DB",
    "/api/media/{source}/{path:path}": "只代理消息源媒体接口，不读 DB",
}

# 静态守卫的探测器：命中即视为「会把内容落成用户可保存文件」
_FILE_MARKERS = ("_export_attachment(", "FileResponse(", "Content-Disposition")


def _client() -> TestClient:
    return TestClient(
        srv.app,
        base_url="http://localhost",
        headers={"Origin": "http://localhost"},
    )


def _mutating_api_routes() -> list[tuple[str, str]]:
    """从 app.routes 枚举 (方法, 具体路径)；路径参数替换为占位值。"""

    out: list[tuple[str, str]] = []
    for route in srv.app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", "")
        if not methods or not path.startswith("/api/"):
            continue
        for method in sorted(set(methods) & _MUTATING):
            out.append((method, _PATH_PARAM_RE.sub("placeholder", path)))
    return out


class TestWindowWriteGate:
    """窗口内变更路由全部 409，且关键写函数与事件发布一次都没被调用。"""

    def test_every_mutating_route_is_blocked_without_side_effects(self):
        routes = _mutating_api_routes()
        assert len(routes) >= 15, f"路由枚举失效（只拿到 {len(routes)} 条）"

        # 只断言 409 不足以证明「零副作用」：闸门判断若写错路径，handler 仍会
        # 在返回 409 之前/之后跑到。故把所有被拦路由会触及的写函数都换成桩，
        # 事后统一断言一次都没被 await。
        item_writes = {
            name: AsyncMock()
            for name in (
                "delete_items",
                "update_items_verify",
                "update_item_verify",
                "update_item_category",
                "toggle_session",
                "trigger_sync",
            )
        }
        category_writes = {
            name: AsyncMock()
            for name in (
                "insert_category",
                "update_category",
                "toggle_category",
                "delete_category",
            )
        }
        item_published = AsyncMock()
        category_published = AsyncMock()
        # import-current 是插件内唯一的 get_items_page 调用点（读临时库 +
        # 覆盖式写用例文件），它的首个 DB 调用不被触发即证明 handler 未运行
        import_current_reads = AsyncMock()

        with (
            patch("briefdesk.db.in_redirect", return_value=True),
            patch.multiple(
                "briefdesk.server.routes_items",
                event_bus=SimpleNamespace(publish=item_published),
                **item_writes,
            ),
            patch.multiple(
                "briefdesk.server.routes_categories",
                event_bus=SimpleNamespace(publish=category_published),
                **category_writes,
            ),
            patch(
                "briefdesk.plugins.benchmark.router.get_items_page",
                import_current_reads,
            ),
        ):
            client = _client()
            blocked: set[tuple[str, str]] = set()
            for method, path in routes:
                if not window_guard.is_blocked_write(method, path):
                    continue
                resp = client.request(method, path, json={})
                assert resp.status_code == 409, f"{method} {path} 未被写闸门拦截"
                assert resp.json()["detail"]["code"] == "benchmark_running"
                blocked.add((method, path))
            client.close()

        # 这些是真实存在且必须被拦下的路由；上表由动态枚举得出，故不存在
        # 「手写清单漏了新路由」的盲区，这里只是防止枚举本身整体失效
        required = {
            ("POST", "/api/items/batch"),
            ("POST", "/api/items/placeholder/verify"),
            ("POST", "/api/items/placeholder/recategorize"),
            ("POST", "/api/sync"),
            ("POST", "/api/sessions/refresh"),
            ("POST", "/api/categories"),
            ("POST", "/api/benchmark/import-current"),
        }
        assert required <= blocked, f"应被拦截却放行: {sorted(required - blocked)}"
        for name, stub in (*item_writes.items(), *category_writes.items()):
            stub.assert_not_awaited()
        item_published.assert_not_awaited()
        category_published.assert_not_awaited()
        import_current_reads.assert_not_awaited()

    def test_cross_origin_still_403_in_window(self):
        """闸门在同源校验之后：跨站请求不得靠 409 探出基准正在运行。"""

        client = TestClient(
            srv.app,
            base_url="http://localhost",
            headers={"Origin": "http://evil.example"},
        )
        with patch("briefdesk.db.in_redirect", return_value=True):
            resp = client.post("/api/items/batch", json={"ids": ["i"], "action": "memo"})
        client.close()
        assert resp.status_code == 403

    def test_whitelist_entries_correspond_to_real_routes(self):
        """白名单不得指向不存在的路由（否则是失效的放行意图）。"""

        actual = {_PATH_PARAM_RE.sub("placeholder", p) for _m, p in _mutating_api_routes()}
        for method, path in window_guard._WINDOW_ALLOWED:
            assert method in _MUTATING
            assert path in actual, f"白名单 {method} {path} 不在 app.routes 中"
        for method, path in _mutating_api_routes():
            if window_guard._WINDOW_ALLOWED_SECRET_DELETE.match(path):
                assert window_guard.is_blocked_write(method, path) is False

    def test_whitelisted_route_reaches_handler_in_window(self):
        """白名单路由必须真的放行到处理器，而不是「恰好不是 409」。"""

        from briefdesk.plugins.benchmark import recorder as bench_recorder

        with patch("briefdesk.db.in_redirect", return_value=True):
            client = _client()
            resp = client.post("/api/benchmark/record", json={"enabled": False})
            client.close()
        assert resp.status_code == 200
        assert bench_recorder.is_enabled() is False


class TestWindowReadBlacklist:
    """窗口内三条产出文件的读路由 409；窗口外与其余读路由不受影响。"""

    def test_file_producing_reads_blocked_in_window(self):
        with patch("briefdesk.db.in_redirect", return_value=True):
            client = _client()
            for path in ("/api/backup", "/api/export/items", "/api/export/recat-samples"):
                resp = client.get(path)
                assert resp.status_code == 409, f"{path} 未被读黑名单拦截"
                assert resp.json()["detail"]["code"] == "benchmark_running"
            # 纯显示类读路由（此处取不读 DB 的内存态端点）不受影响
            assert client.get("/api/benchmark/run").status_code == 200
            client.close()

    def test_blacklist_and_whitelist_are_disjoint(self):
        assert not (window_guard._WINDOW_BLOCKED_READS & set(_FILELESS_READ_WHITELIST))
        for path in window_guard._WINDOW_BLOCKED_READS:
            assert path.startswith("/api/")


class TestExportAttachmentGuard:
    """导出产出点守卫与名单无关：窗口内一律拒绝，窗口外正常产出。"""

    def test_guard_raises_in_window(self):
        with (
            patch("briefdesk.db.in_redirect", return_value=True),
            pytest.raises(HTTPException) as excinfo,
        ):
            srv_routes._export_attachment("a,b", "text/csv", "x.csv")
        assert excinfo.value.status_code == 409
        assert excinfo.value.detail["code"] == "benchmark_running"

    def test_attachment_returned_outside_window(self):
        with patch("briefdesk.db.in_redirect", return_value=False):
            resp = srv_routes._export_attachment("a,b", "text/csv", "x.csv")
        assert resp.headers["content-disposition"] == 'attachment; filename="x.csv"'
        assert resp.body == b"a,b"

    async def test_guard_holds_inside_real_window(self, tmp_path):
        async with db_redirect(str(tmp_path / "bench.sqlite")):
            with pytest.raises(HTTPException):
                srv_routes._export_attachment("a", "text/csv", "x.csv")


def _decorated_get_paths(func: ast.AST) -> list[str]:
    """函数的 @app.get / @router.get 装饰器路径（其余装饰器忽略）。"""

    out: list[str] = []
    for dec in getattr(func, "decorator_list", []):
        if isinstance(dec, ast.Call):
            node: ast.AST = dec.func
            args = dec.args
        else:
            node, args = dec, []
        if isinstance(node, ast.Attribute) and node.attr in ("get", "api_route"):
            for arg in args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    out.append(arg.value)
    return out


def _file_producing_get_handlers() -> list[tuple[str, str, list[str], list[str]]]:
    """扫描核心 server/** 与插件 plugins/** 的 GET 处理器。

    只看被路由装饰器装饰的函数：static.py 中 _SpaStaticFiles.get_response 的
    FileResponse 不属路由处理器，不纳入。探测器不含 StreamingResponse，否则
    会误伤 SSE 流。
    """

    out: list[tuple[str, str, list[str], list[str]]] = []
    files = sorted((_BRIEFDESK / "server").rglob("*.py")) + sorted(
        (_BRIEFDESK / "plugins").rglob("*.py")
    )
    for path in files:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            paths = _decorated_get_paths(node)
            if not paths:
                continue
            segment = ast.get_source_segment(text, node) or ""
            hits = [marker for marker in _FILE_MARKERS if marker in segment]
            if hits:
                out.append(
                    (str(path.relative_to(_ROOT)), node.name, paths, hits)
                )
    return out


class TestFileProducingRouteRegistry:
    """静态守卫：会产出文件的 GET 处理器必须登记。

    读路由是**默认放行**口径，新增下载接口没有任何运行时提示——这是唯一能
    拦住「将来新增文件型读路由却忘了登记」的机制。
    """

    def test_every_file_producing_get_handler_is_registered(self):
        handlers = _file_producing_get_handlers()
        assert handlers, "扫描未命中任何处理器，探测器可能已失效"
        allowed = set(window_guard._WINDOW_BLOCKED_READS) | set(
            _FILELESS_READ_WHITELIST
        )
        unregistered = [
            (src, name, path, hits)
            for src, name, paths, hits in handlers
            for path in paths
            if path not in allowed
        ]
        assert unregistered == [], (
            "以下会产出文件的 GET 处理器未登记到 window_guard 名单："
            f"{unregistered}（若确实不读 DB，请加入白名单并写明理由）"
        )

    def test_whitelist_entries_are_documented(self):
        for path, reason in _FILELESS_READ_WHITELIST.items():
            assert reason, f"{path} 的白名单理由不得为空"

    def test_detector_ignores_streaming_response(self):
        assert not any("StreamingResponse" in m for m in _FILE_MARKERS)


class TestRagStatusDegrade:
    """窗口内 /api/rag/status 降级返回，窗口外补 available: True。"""

    async def test_degrades_in_window(self, tmp_path):
        from briefdesk.plugins.rag.config import RagSettings
        from briefdesk.plugins.rag.engine import RagEngine, set_engine

        engine = RagEngine(RagSettings())
        set_engine(engine)
        try:
            async with db_redirect(str(tmp_path / "bench.sqlite")):
                data = await _rag_status()
        finally:
            set_engine(None)

        assert data["available"] is False
        assert data["chunks"] == 0
        assert data["embedded"] == 0
        assert data["backfill_days"] == engine.settings.backfill_days

    async def test_available_outside_window(self):
        from briefdesk.plugins.rag.config import RagSettings
        from briefdesk.plugins.rag.engine import RagEngine, set_engine

        engine = RagEngine(RagSettings())
        set_engine(engine)
        try:
            with (
                patch(
                    "briefdesk.plugins.rag.router.get_db", AsyncMock(return_value=object())
                ),
                patch(
                    "briefdesk.plugins.rag.router.count_status",
                    AsyncMock(
                        return_value={
                            "rag_chunks": 3,
                            "rag_chunk_embeddings": 2,
                            "fts_tokenizer": "unicode61",
                        }
                    ),
                ),
            ):
                data = await _rag_status()
        finally:
            set_engine(None)

        assert data["available"] is True
        assert data["chunks"] == 3
        assert data["embedded"] == 2


async def _rag_status() -> dict:
    import briefdesk.plugins.rag.router as rag_router

    return await rag_router.rag_status()

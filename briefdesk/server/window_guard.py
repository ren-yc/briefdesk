"""基准窗口内的 HTTP 契约：写路由白名单、读路由黑名单与统一的 409 响应。

集中放在此处的理由：写闸门（middleware）、读黑名单（middleware）、导出产出点
守卫（routes_items）与备份数据库层防线（db）必须给出同一份文案与同一形状的
detail；分散写会随时间漂移，前端按 detail.code 分流也会跟着失效。

本模块只含常量与纯函数，**不 import briefdesk.db / briefdesk.server.app**：
既避免循环导入，也让测试可直接 import 而不必装配整个应用。
"""

import re

from fastapi.responses import JSONResponse

# 窗口内**放行**的变更路由（方法 + 路径逐条列举）。
#
# 逐条列举而非用 "/api/benchmark/" 前缀通配是刻意的：/api/benchmark/import-current
# 读的是被重定向后的临时库（内含基准合成卡），并**覆盖式**写 cases/*.fromweb.json，
# 前缀通配会把它一并放行，用户的真实用例数据集会被合成数据覆盖。
#
# 放行依据：
# - /api/settings/env 与 /api/settings/secrets：只读写启动配置文件与密钥库，不碰 DB；
# - /api/restore：上传文件校验后仅暂存为 {db_path}.restore-pending，重启才生效；
# - /api/benchmark/run：基准运行入口本身；
# - /api/benchmark/record、export-recorded、cases：进入基准环境前用例已全部读完，
#   这几条只操作内存记录或落盘用例文件，不影响本次运行。
_WINDOW_ALLOWED: frozenset[tuple[str, str]] = frozenset(
    {
        ("PUT", "/api/settings/env"),
        ("POST", "/api/settings/secrets"),
        ("POST", "/api/restore"),
        ("POST", "/api/benchmark/run"),
        ("POST", "/api/benchmark/record"),
        ("DELETE", "/api/benchmark/record"),
        ("POST", "/api/benchmark/export-recorded"),
        ("DELETE", "/api/benchmark/cases"),
    }
)

# /api/settings/secrets/{name} 的 DELETE 带路径参数，无法逐条穷举，按形态匹配
_WINDOW_ALLOWED_SECRET_DELETE = re.compile(r"^/api/settings/secrets/[^/]+$")

# 窗口内**拦截**的读路由（显式黑名单）。
#
# 读侧与写侧口径相反是刻意的：纯显示类读路由在窗口内显示临时库内容，影响面
# 已被列表区提示覆盖，无需逐一登记；只有会产出「用户可保存文件」的这三条必须
# 拦——它们把临时库内容落成用户手里的文件，备份更是「日后恢复即整库被临时库
# 替换」，而 validate_restore_file 对临时库会全部放行。因此新增会产出文件的
# 读路由必须登记到本名单：读侧是默认放行口径，**不登记就等于无保护**。
_WINDOW_BLOCKED_READS: frozenset[str] = frozenset(
    {
        "/api/backup",
        "/api/export/items",
        "/api/export/recat-samples",
    }
)


def benchmark_busy_detail() -> dict[str, str]:
    """窗口期 409 的 detail（写闸门 / 读黑名单 / 导出守卫 / 备份防线四处共用）。

    code 与公告码同名是有意的：两者同指一个窗口，便于关联排查。
    """

    return {
        "code": "benchmark_running",
        "message": "基准运行中：界面写操作与备份/导出暂不可用，请等运行结束后重试",
    }


def benchmark_busy_response() -> JSONResponse:
    """窗口期 409 响应。

    形状与导出产出点守卫抛出的 HTTPException(409, detail=...) 同构
    （FastAPI 默认处理器产出 {"detail": ...}），前端只需一套解析。
    """

    return JSONResponse({"detail": benchmark_busy_detail()}, status_code=409)


def is_blocked_write(method: str, path: str) -> bool:
    """变更请求是否应被窗口闸门拒绝（默认拒绝 + 显式白名单放行）。"""

    return (method, path) not in _WINDOW_ALLOWED and not (
        method == "DELETE" and _WINDOW_ALLOWED_SECRET_DELETE.match(path)
    )


def is_blocked_read(method: str, path: str) -> bool:
    """读请求是否应被窗口闸门拒绝（默认放行 + 显式黑名单拦截）。"""

    return method == "GET" and path in _WINDOW_BLOCKED_READS

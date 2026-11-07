"""嵌入模型迁移助手（嵌入通道换模型/换维度时的人工步骤收口）。

用途：把去重向量通道迁到新嵌入模型。核心库的缓存重建只在「换模型名」时
自愈（按模型名取不到向量 → 全量重嵌），「同名换维度」永不自愈；rag 侧
窗口回填默认只覆盖近 7 天。本脚本把迁移期的人工步骤收进一个带校验的
入口，防手改配置 / 手删向量行漏步。

执行顺序（应用停机窗口内操作；步骤详单见脚本结尾输出）：
  1. python scripts/migrate_embed_model.py --check
     只读盘点：现有向量按模型/维度分组计数、目标模型与端点、库路径来源。
  2. python scripts/migrate_embed_model.py --execute --purge-old-rows \
         --new-model <目标模型> --embed-base http://localhost:11434/v1 [--yes]
     前置校验（目标模型已知、端点可达、返回维度一致）后：在线备份库文件
     为一致快照，再清空向量表（迁移 = 全量重嵌，不留任何旧模型/旧维度
     行；「无行可清」时拒绝执行防误删）。

安全设计：库被占用（应用未停）时读取直接报错退出，绝不按零行继续；
目标模型未知时拒绝清库（否则所有行都会被误判成「旧模型残留」）；
备份用 SQLite 在线备份 API 产出单文件一致快照（不受 -wal/-shm 影响），
带时间戳、不覆盖历史备份。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

EMBED_TABLE = "item_embeddings"
# 嵌入端点响应的单次读取上限：正常向量远小于此，防端点异常时无限读入
_PROBE_READ_LIMIT = 1_000_000


def load_env_file(path: Path) -> dict[str, str]:
    """解析 dotenv 文件的 KEY=VALUE 行（近似 pydantic-settings 的 dotenv 口径：
    utf-8-sig 解码、export 前缀、未加引号值剥行内注释；忽略注释/空行）。"""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.sub(r"\s+#.*$", "", value).strip()
        out[key.strip()] = value
    return out


def _dotenv_layers() -> list[tuple[str, Path]]:
    """运行时的唯一有序 dotenv 层定义（项目 .env > 用户暂存 settings.env）。

    直接复用 briefdesk.settings_env.dotenv_layers()：库路径的层序若在本
    脚本另抄一份，运行时调整层序后必然漂移（清错库正是要防的事故）。
    """
    from briefdesk.settings_env import dotenv_layers

    return dotenv_layers()


def _default_db_path() -> Path:
    """运行时的默认库路径（内部按 BRIEFDESK_DATA_DIR → 平台目录解析）。"""
    from briefdesk.paths import database_path

    return database_path()


def resolve_db_path(explicit: Path | None) -> tuple[Path, str]:
    """解析库路径并返回 (路径, 来源说明)，与运行时配置解析链同序：
    显式 --db > 进程环境 DB_PATH > 项目 .env > 用户暂存 settings.env >
    默认（paths.database_path：BRIEFDESK_DATA_DIR → 平台目录）。

    键名一律**大小写不敏感**（与 pydantic-settings 及 settings_env._env_value
    同口径）：运行时可解析 `db_path` 这类小写变体，本脚本若按大写精确匹配就会
    漏读——应用连 A 库、`--check`/`--execute` 却盘点/清空平台默认库，是能真
    删错库的事故面。

    有意偏差：运行时把空串值视为「已设置」，本脚本视为「未设置」继续
    查低层——迁移场景下空串几乎必然是残留配置，报「不存在」比拿空串
    当路径更可用。
    """
    if explicit is not None:
        return explicit, "--db 参数"
    value = _env_lookup(os.environ, "DB_PATH")
    if value:
        return Path(value), "环境变量 DB_PATH"
    for layer_name, layer_file in _dotenv_layers():
        value = _env_lookup(load_env_file(layer_file), "DB_PATH") or ""
        if value:
            path = Path(value)
            if not path.is_absolute():
                path = path.resolve()
            return path, f"{layer_name}（{layer_file}）"
    return _default_db_path(), "默认（paths.database_path）"


def _env_lookup(env: Mapping[str, str], key: str) -> str | None:
    """大小写不敏感取键值（同名多形态时精确大写优先，其余按遍历序）。

    与 settings_env._env_value 同语义：先精确命中，再逐项 upper() 比对。
    空串算「有值」——本脚本在调用点用 `or ""` 判断真值，保持既有偏差
    （空串视为未设置继续查低层）。
    """
    direct = env.get(key)
    if direct is not None:
        return direct
    target = key.upper()
    for name, value in env.items():
        if name.upper() == target:
            return value
    return None


def summarize_rows(
    rows: list[tuple[str, str, str]],
) -> list[tuple[str, int, int]]:
    """把向量行按 (model, 维度) 分组计数。

    rows 为 (model, embedding_json, item_id) 三元组；embedding 解析失败
    单独计入 "<bad_json>" 组——这本身就是需要重建的信号。
    """
    groups: dict[tuple[str, int], int] = {}
    bad_json = 0
    for model, embedding_json, _item_id in rows:
        try:
            dim = len(json.loads(embedding_json))
        except (json.JSONDecodeError, TypeError):
            bad_json += 1
            continue
        key = (model, dim)
        groups[key] = groups.get(key, 0) + 1
    summary = [(model, dim, count) for (model, dim), count in sorted(groups.items())]
    if bad_json:
        summary.append(("<bad_json>", -1, bad_json))
    return summary


def read_env_embedding_config(env: dict[str, str]) -> tuple[str, str, bool]:
    """取 (生效模型名, 嵌入端点, 模型名是否来自 AI_MODEL 回退)。

    运行时的嵌入模型名是 EMBED_MODEL 留空回退 AI_MODEL——盘点分组必须
    用同一个生效值，否则 EMBED_MODEL 未配时会把行错误地当成「旧模型残留」。
    """
    model = env.get("EMBED_MODEL", "")
    from_ai_model = False
    if not model and env.get("AI_MODEL", ""):
        model = env["AI_MODEL"]
        from_ai_model = True
    return model, env.get("EMBED_API_BASE", ""), from_ai_model


def _load_effective_env() -> dict[str, str]:
    """按运行时层序合并 dotenv 层（高层覆盖低层），供模型/端点取值。

    取数与运行时**同口径**（否则盘点会把真实模型行误报成旧模型残留）：
    - 键名大小写不敏感：pydantic-settings 与 settings_env._env_value 都按
      upper() 比对；os.environ.get("EMBED_MODEL") 会漏掉 embed_model 这类
      变体，导致进程环境的值被当成未设置。
    - ${VAR} 插值只作用于项目 .env 层（dotenv 语义层）；用户暂存层按字面量
      解析（运行时解析链对暂存层关闭插值），其值不参与变量展开——若对暂存
      值也插值，脚本看到的模型名会与真实生效值分叉，盘点结论失真。
    插值变量集 = 项目 .env 已合并值 + 进程环境（大小写不敏感）；未定义的
    变量按空串处理（与 python-dotenv 的默认一致）。
    """
    merged: dict[str, str] = {}
    interpolated_keys: set[str] = set()
    for layer_name, layer_file in reversed(_dotenv_layers()):
        layer_values = load_env_file(layer_file)
        merged.update(layer_values)
        if layer_name != "override":
            interpolated_keys.update(layer_values)
    process_env = {key.upper(): value for key, value in os.environ.items()}
    for key in ("EMBED_MODEL", "EMBED_API_BASE", "AI_MODEL"):
        value = process_env.get(key.upper())
        if value:
            merged[key] = value
    lookup = {
        key.upper(): value for key, value in {**merged, **process_env}.items()
    }
    interpolated: dict[str, str] = {}
    for key, value in merged.items():
        if key in interpolated_keys:
            interpolated[key] = _interpolate_env_value(value, lookup)
        else:
            interpolated[key] = value
    return interpolated


_ENV_VAR_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate_env_value(value: str, lookup: dict[str, str]) -> str:
    """把 ${VAR} 引用替换为 lookup 里的值（未定义 → 空串，python-dotenv 口径）。"""

    def _sub(match: re.Match[str]) -> str:
        return lookup.get(match.group(1).upper(), "")

    return _ENV_VAR_REF.sub(_sub, value)


def _parse_probe_response(body: object) -> int:
    """从已解析的嵌入响应中取出向量维度；形状不对抛 RuntimeError。"""
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"嵌入端点响应缺 data 数组：{body!r:.200}")
    first = data[0]
    embedding = first.get("embedding") if isinstance(first, dict) else None
    if not isinstance(embedding, list) or not embedding:
        raise RuntimeError(f"嵌入端点响应缺向量字段：{body!r:.200}")
    return len(embedding)


def probe_embedding_dimension(base: str, model: str, timeout: float = 30.0) -> int:
    """请求一次嵌入端点，返回向量维度；任何失败抛 RuntimeError。

    仅接受 http(s) 端点；显式绕过环境代理（本脚本面向本地端点，走代理会
    把回环地址探测挂到超时）；响应读取设上限；解码/传输/协议异常统一
    收口为 RuntimeError。仅面向无鉴权的本地端点（脚本不发送凭据）。
    """
    url = base.rstrip("/") + "/embeddings"
    if not url.lower().startswith(("http://", "https://")):
        raise RuntimeError(f"仅支持 http(s) 嵌入端点，收到：{url}")
    payload = json.dumps({"model": model, "input": "迁移前维度探测"}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(_PROBE_READ_LIMIT + 1)
        if len(raw) > _PROBE_READ_LIMIT:
            raise RuntimeError(f"嵌入端点响应超过 {_PROBE_READ_LIMIT} 字节读取上限")
        body = json.loads(raw)
    except RuntimeError:
        raise
    except Exception as e:  # 端点侧任何失败统一转 RuntimeError（含解码/传输层）
        raise RuntimeError(f"嵌入端点探测失败（{url}，模型 {model}）：{e}") from e
    return _parse_probe_response(body)


def plan_migration(
    existing: list[tuple[str, int, int]],
    new_model: str,
    new_dim: int,
) -> list[str]:
    """根据盘点结果生成迁移前校验结论；返回问题列表（空 = 无行可清）。

    new_dim 为 0 表示未探测（--skip-probe），此时只按模型名对比。
    new_model 为空时任何行都无法判定归属——整体报「目标模型未配置」，
    绝不把全部行误判成「旧模型残留」（那会导致清空全表还报成功）。
    """
    problems: list[str] = []
    if not new_model and existing:
        problems.append(
            "目标模型未配置（--new-model / .env EMBED_MODEL 或 AI_MODEL），"
            "无法判定哪些行是残留；拒绝清库"
        )
        return problems
    for model, dim, count in existing:
        if model == "<bad_json>":
            problems.append(f"存在 {count} 行向量 JSON 无法解析，必须重建")
        elif model != new_model:
            problems.append(
                f"旧模型行残留：model={model} dim={dim} count={count}（将清理）"
            )
        elif new_dim > 0 and dim != new_dim:
            problems.append(
                f"同名模型维度不一致：库内 {dim} vs 端点 {new_dim}"
                "（同名换维度不自愈，必须清行重建）"
            )
    return problems


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    conn_result = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return conn_result is not None


def fetch_embedding_rows(db_path: Path) -> list[tuple[str, str, str]]:
    """读取向量行；表不存在返回空列表，其它读取失败抛 RuntimeError。

    区分「表不存在（全新库，等价零行）」与「库读不了（应用未停/路径写
    错/非 SQLite 文件）」：后者按错误处理退出，绝不当作零行给出假绿灯。
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(db_path)
        try:
            if not table_exists(conn, EMBED_TABLE):
                return []
            return conn.execute(
                f"SELECT model, embedding, item_id FROM {EMBED_TABLE}"
            ).fetchall()
        except sqlite3.DatabaseError as e:
            raise RuntimeError(
                f"读取 {EMBED_TABLE} 失败：{e}"
                "（库可能被占用——确认应用已停止；或文件不是 SQLite 库/路径写错）"
            ) from e
    finally:
        if conn is not None:
            conn.close()


def backup_database(db_path: Path) -> Path:
    """SQLite 在线备份为带时间戳的单文件一致快照，返回备份路径。

    先用 BEGIN IMMEDIATE 短超时探测占用：库被独占时在线备份会在
    sqlite3_backup_step 上无限重试（表现为挂死而非报错），必须先把它
    变成显式失败。在线备份读的是提交快照，不受 -wal/-shm 半程状态影响；
    时间戳命名、已存在即报错，绝不覆盖历史备份。
    恢复口径：把备份文件拷回原路径，并删除同目录残留的 -wal/-shm 再启动。
    """
    backup = db_path.with_name(
        f"{db_path.name}.bak-embed-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    if backup.exists():
        raise RuntimeError(f"备份文件已存在，拒绝覆盖：{backup}")
    src: sqlite3.Connection | None = None
    try:
        src = sqlite3.connect(db_path)
        try:
            src.execute("PRAGMA busy_timeout = 2000")
            src.execute("BEGIN IMMEDIATE")
            src.commit()
        except sqlite3.DatabaseError as e:
            raise RuntimeError(
                f"库被占用，无法安全备份：{e}（确认应用已停止后重试）"
            ) from e
        dst = sqlite3.connect(backup)
        try:
            src.backup(dst)
        except sqlite3.DatabaseError as e:
            # BEGIN IMMEDIATE 探测与备份之间存在占用窗口（TOCTOU）：
            # 在线备份遇锁会在 backup_step 无限重试，届时以异常形式失败，
            # 半截备份文件留给操作员人工删除
            raise RuntimeError(f"在线备份失败：{e}（确认应用已停止后重试）") from e
        finally:
            dst.close()
    finally:
        if src is not None:
            src.close()
    return backup


def purge_all_embeddings(db_path: Path) -> int:
    """清空向量表，返回删除行数；表不存在视为零行。"""
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(db_path)
        try:
            if not table_exists(conn, EMBED_TABLE):
                return 0
            cursor = conn.execute(f"DELETE FROM {EMBED_TABLE}")
            deleted = cursor.rowcount
            conn.commit()
            return deleted
        except sqlite3.DatabaseError as e:
            raise RuntimeError(
                f"清空 {EMBED_TABLE} 失败：{e}"
                "（库可能被占用——确认应用已停止；或文件不是 SQLite 库/路径写错）"
            ) from e
    finally:
        if conn is not None:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--db", type=Path, default=None, help="库文件路径（默认按运行时解析链推导）"
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=None,
        help="（兼容保留）单份 .env 路径；默认改走运行时 dotenv 层序",
    )
    parser.add_argument("--check", action="store_true", help="只读盘点，不改动")
    parser.add_argument("--execute", action="store_true", help="执行备份与清理")
    parser.add_argument("--new-model", default="", help="迁移目标嵌入模型名")
    parser.add_argument("--embed-base", default="", help="目标嵌入端点（/v1 结尾）")
    parser.add_argument(
        "--purge-old-rows", action="store_true", help="确认清空向量表（全量重嵌）"
    )
    parser.add_argument("--skip-probe", action="store_true", help="跳过端点维度探测")
    parser.add_argument("--yes", action="store_true", help="跳过执行前确认")
    args = parser.parse_args(argv)

    # 执行形态的门在参数层收口，任何库/网络访问之前
    if args.execute and not args.purge_old_rows:
        parser.error("--execute 需要 --purge-old-rows（迁移 = 清空全表 + 全量重嵌）")
    if args.check and args.execute:
        parser.error("--check 与 --execute 互斥（避免「以为清了其实只读」）")

    if args.env is not None:
        env = load_env_file(args.env)
    else:
        env = _load_effective_env()
    env_model, env_base, model_from_fallback = read_env_embedding_config(env)
    new_model = args.new_model or env_model
    embed_base = args.embed_base or env_base

    # 执行前目标模型必须已知：未知时所有行都无法判定归属，清库即误删
    if args.execute and not new_model:
        print("目标模型未知（--new-model / .env EMBED_MODEL 或 AI_MODEL），拒绝清库")
        return 2

    db_path, db_source = resolve_db_path(args.db)
    if not db_path.is_file():
        print(f"库文件不存在：{db_path}（来源：{db_source}）")
        return 2

    try:
        rows = fetch_embedding_rows(db_path)
    except RuntimeError as e:
        print(str(e))
        return 2

    existing = summarize_rows(rows)
    print(f"库文件：{db_path}（来源：{db_source}）")
    print(f"向量行合计：{len(rows)}")
    for model, dim, count in existing:
        print(f"  model={model}  dim={dim}  count={count}")
    model_note = "（回退 AI_MODEL，与运行时生效值一致）" if model_from_fallback else ""
    print(
        f"目标模型：{new_model or '<未配置>'}{model_note}"
        f"  端点：{embed_base or '<未配置>'}"
    )
    if args.skip_probe and new_model and any(
        model == new_model for model, _dim, _count in existing
    ):
        print("警告：--skip-probe 跳过了维度探测，同名行的维度一致性未验证")

    new_dim = 0
    if args.execute and not args.skip_probe:
        if not embed_base:
            print("执行需要 --embed-base 或配置 EMBED_API_BASE")
            return 2
        try:
            new_dim = probe_embedding_dimension(embed_base, new_model)
        except RuntimeError as e:
            print(f"迁移中止：{e}")
            return 2
        print(f"端点探测：model={new_model} dim={new_dim}")

    problems = plan_migration(existing, new_model, new_dim)
    if problems:
        print("迁移前校验发现：")
        for p in problems:
            print(f"  - {p}")
    else:
        print("迁移前校验：无残留问题")

    if not args.execute:
        print("（--check 只读模式，未做任何改动）")
        return 0

    if not problems:
        # 没有可清理的对象：拒绝空迁移防误删。分档文案帮操作员判断下一步。
        if not rows:
            print("向量表为空（或表不存在），无需迁移")
        elif args.skip_probe:
            print("未探测端点维度，无法判定同名换维度；去掉 --skip-probe 并补"
                  " --embed-base 后重试（确认无需迁移则到此为止）")
        else:
            print("没有需要清理的旧行（库内已是目标模型且维度一致），拒绝空迁移")
        return 2
    if not args.yes:
        try:
            answer = input(f"将备份并清空 {db_path} 的 {EMBED_TABLE} 表，确认？[y/N] ")
        except EOFError:
            print("非交互环境无确认输入，已取消（确定执行请加 --yes）")
            return 1
        if answer.strip().lower() != "y":
            print("已取消")
            return 1

    try:
        backup = backup_database(db_path)
    except RuntimeError as e:
        print(f"迁移中止：{e}")
        return 2
    deleted = purge_all_embeddings(db_path)
    print(f"已备份库文件 → {backup}")
    print(f"已清空 {EMBED_TABLE}（删除 {deleted} 行）")
    print("恢复口径：把备份文件拷回原路径，删除同目录残留的 -wal/-shm 后再启动")
    print("后续人工步骤：")
    print("  1. 确认 EMBED_MODEL / EMBED_API_BASE 已指向新模型")
    print("  2. 启动应用（去重缓存按新模型名全量重嵌）")
    print("  3. 临时设 RAG_BACKFILL_DAYS=-1 触发 rag 全量回填，完成后恢复原值")
    print("  4. 验收：/api/rag/status 与去重遥测确认无维度告警")
    return 0


if __name__ == "__main__":
    sys.exit(main())

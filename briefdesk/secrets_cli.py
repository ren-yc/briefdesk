"""briefdesk secrets 子命令 — 系统密钥环管理（keyring）。

用法:
    briefdesk secrets set <NAME> [VALUE]   写入密钥（VALUE 缺省时安全提示输入，
                                          不落 shell 历史）；写入后重启生效
    briefdesk secrets get <NAME> [--reveal] 查询状态；--reveal 才打印明文
    briefdesk secrets rm <NAME>            删除密钥（幂等）
    briefdesk secrets list                 列出全部可管理密钥的配置状态

NAME 白名单见 secrets_store.SECRET_NAMES（env 风格命名，与 .env 对齐）。
密钥只写入系统密钥环，绝不回写 .env 明文文件；无桌面会话/无 Secret Service
时可用环境变量或 .env 作为回退（见 briefdesk/secrets_store.py）。
"""

import argparse
import getpass
import sys

from briefdesk.secrets_store import (
    SECRET_NAMES,
    SecretsStoreError,
    delete_secret,
    get_secret,
    set_secret,
)


def _valid_name(name: str) -> str:
    name = name.upper()
    if name not in SECRET_NAMES:
        raise SystemExit(f"未知密钥名 {name!r}（可用: {', '.join(SECRET_NAMES)}）")
    return name


def _cmd_set(args: argparse.Namespace) -> int:
    name = _valid_name(args.name)
    value = args.value
    if value is None:
        value = getpass.getpass(f"{name}: ")
    set_secret(name, value)
    print(f"{name} 已写入系统密钥环（重启生效）")
    return 0


def _cmd_get(args: argparse.Namespace) -> int:
    name = _valid_name(args.name)
    value = get_secret(name)
    if value is None:
        print(f"{name}: 未配置")
        return 1
    if args.reveal:
        print(value)
    else:
        print(f"{name}: 已配置 (长度 {len(value)})")
    return 0


def _cmd_rm(args: argparse.Namespace) -> int:
    name = _valid_name(args.name)
    delete_secret(name)
    print(f"{name} 已从系统密钥环删除")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    for name in SECRET_NAMES:
        print(f"{name}: {'已配置' if get_secret(name) is not None else '未配置'}")
    return 0


def secrets_cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="briefdesk secrets",
        description="管理系统密钥环中的简报台密钥（keyring，Windows=凭据管理器/DPAPI）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_set = sub.add_parser("set", help="写入密钥到系统密钥环（重启生效）")
    p_set.add_argument("name", help="密钥名（白名单见 secrets_store.SECRET_NAMES）")
    p_set.add_argument(
        "value", nargs="?", default=None,
        help="密钥值；缺省时安全提示输入（不落 shell 历史）",
    )
    p_set.set_defaults(func=_cmd_set)

    p_get = sub.add_parser("get", help="查询密钥配置状态")
    p_get.add_argument("name", help="密钥名")
    p_get.add_argument(
        "--reveal", action="store_true", help="打印明文（默认仅显示是否配置与长度）"
    )
    p_get.set_defaults(func=_cmd_get)

    p_rm = sub.add_parser("rm", help="从系统密钥环删除密钥（幂等）")
    p_rm.add_argument("name", help="密钥名")
    p_rm.set_defaults(func=_cmd_rm)

    p_list = sub.add_parser("list", help="列出全部可管理密钥的配置状态")
    p_list.set_defaults(func=_cmd_list)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SecretsStoreError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(secrets_cli_main(sys.argv[1:]))
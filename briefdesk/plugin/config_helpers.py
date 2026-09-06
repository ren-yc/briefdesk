"""插件配置验证助手函数。"""

from typing import Any

from briefdesk.plugin.base import PluginDisabledError


def validate_required_config(settings, required_fields: dict[str, str]) -> None:
    """验证必填配置字段，缺失时抛 PluginDisabledError。

    Args:
        settings: 配置对象
        required_fields: {字段名: 环境变量名} 映射

    Raises:
        PluginDisabledError: 缺少必填配置时

    Example:
        validate_required_config(settings, {
            'api_token': 'QQFLOW_API_TOKEN',
            'qq': 'QQFLOW_QQ',
            'key': 'QQFLOW_KEY',
        })
    """
    missing = []
    for field_name, env_name in required_fields.items():
        # Any 注解：getattr 缺省 None 会让 mypy 推出 Any | None，
        # 对 None 谓 hasattr 误报 union-attr；非 str 标量（dict 等）原样参与真值判定
        value: Any = getattr(settings, field_name, None)
        # SecretStr 需要调用 get_secret_value()
        if hasattr(value, "get_secret_value"):
            value = value.get_secret_value()
        if not value:
            missing.append(env_name)

    if missing:
        raise PluginDisabledError(
            f"缺少必填配置 {', '.join(missing)}"
            "（在 .env / 系统密钥环中配置后重启生效）"
        )

"""API 设置读写服务。"""

from decimal import Decimal
from ipaddress import ip_address

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator

from writing_companion.services.credentials import CredentialStore
from writing_companion.storage.database import Database


class ApiSettings(BaseModel):
    """可落盘到 SQLite 的 API 设置，不包含 API key。"""

    model_config = ConfigDict(extra="forbid")

    base_url: AnyHttpUrl
    model: str
    image_input_enabled: bool
    context_limit: int = Field(gt=0)
    daily_cost_limit: Decimal = Field(ge=Decimal("0"))

    @field_validator("base_url")
    @classmethod
    def validate_base_url_transport(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        """拒绝公网 HTTP，避免明文发送 Bearer key。"""

        ensure_secure_or_loopback_http(value)
        return value


class ApiSettingsService:
    """管理 API 设置与凭据状态。"""

    def __init__(self, database: Database, credential_store: CredentialStore) -> None:
        self.database = database
        self.credential_store = credential_store

    def save(self, settings: ApiSettings, api_key: str | None = None) -> bool:
        """保存 API 设置；仅当传入 API key 时写入凭据管理器。"""

        values = {
            "api.base_url": str(settings.base_url).rstrip("/"),
            "api.model": settings.model,
            "api.image_input_enabled": "true"
            if settings.image_input_enabled
            else "false",
            "api.context_limit": str(settings.context_limit),
            "api.daily_cost_limit": _format_decimal(settings.daily_cost_limit),
        }
        with self.database.transaction() as connection:
            for key, value in values.items():
                connection.execute(
                    """
                    INSERT INTO settings(key, value, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (key, value),
                )
        if api_key:
            self.credential_store.save_api_key(api_key)
        return self.is_configured()

    def load(self) -> ApiSettings | None:
        """读取已保存的 API 设置；不存在时返回 None。"""

        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT key, value FROM settings WHERE key LIKE 'api.%'"
            ).fetchall()
        values = {row[0]: row[1] for row in rows}
        required_keys = {
            "api.base_url",
            "api.model",
            "api.image_input_enabled",
            "api.context_limit",
            "api.daily_cost_limit",
        }
        if not required_keys.issubset(values):
            return None
        return ApiSettings(
            base_url=values["api.base_url"],
            model=values["api.model"],
            image_input_enabled=values["api.image_input_enabled"] == "true",
            context_limit=int(values["api.context_limit"]),
            daily_cost_limit=Decimal(values["api.daily_cost_limit"]),
        )

    def load_api_key(self) -> str | None:
        """读取 API key，供连接测试使用。"""

        return self.credential_store.load_api_key()

    def is_configured(self) -> bool:
        """判断本写作库是否已有 API key。"""

        api_key = self.credential_store.load_api_key()
        return bool(api_key)


def _format_decimal(value: Decimal) -> str:
    """用普通小数字符串保存金额上限。"""

    return format(value, "f")


def ensure_secure_or_loopback_http(value: AnyHttpUrl) -> None:
    """仅允许 HTTPS；本机开发可使用 loopback HTTP。"""

    if value.scheme == "https":
        return
    if value.scheme == "http" and _is_loopback_host(value.host or ""):
        return
    raise ValueError("公网 API 地址必须使用 HTTPS；仅本机 loopback 可使用 HTTP")


def _is_loopback_host(host: str) -> bool:
    """判断主机名是否为本机 loopback。"""

    normalized = host.strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False

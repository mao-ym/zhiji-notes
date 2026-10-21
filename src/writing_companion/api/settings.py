"""API 设置 HTTP 路由。"""

from decimal import Decimal
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    field_validator,
    ValidationError,
)

from writing_companion.services.settings import (
    ApiSettings,
    ApiSettingsService,
    ensure_secure_or_loopback_http,
)
from writing_companion.services.credentials import CredentialStoreError


router = APIRouter(prefix="/api/settings/api", tags=["settings"])


class ApiSettingsRequest(BaseModel):
    """保存 API 设置请求体，API key 不会写入 SQLite。"""

    model_config = ConfigDict(extra="forbid")

    base_url: AnyHttpUrl
    model: StrictStr
    image_input_enabled: StrictBool
    context_limit: int = Field(gt=0)
    daily_cost_limit: Decimal = Field(ge=Decimal("0"))
    api_key: StrictStr | None = None

    @field_validator("base_url")
    @classmethod
    def validate_base_url_transport(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        """拒绝公网 HTTP，避免连接测试明文发送 Bearer key。"""

        ensure_secure_or_loopback_http(value)
        return value


class ApiSettingsResponse(BaseModel):
    """API 设置保存响应，只暴露是否已配置凭据。"""

    configured: bool


class ApiSettingsStatusResponse(BaseModel):
    """只返回 API 是否已经完成配置。"""

    configured: bool


class ApiConnectionTestResponse(BaseModel):
    """API 连接测试结果。"""

    ok: bool
    error_type: str | None = None
    message: str


def get_api_settings_service(request: Request) -> ApiSettingsService:
    """从应用容器获取 API 设置服务。"""

    return request.app.state.container["api_settings_service"]


@router.get("/status", response_model=ApiSettingsStatusResponse)
def get_api_settings_status(
    service: Annotated[ApiSettingsService, Depends(get_api_settings_service)],
) -> ApiSettingsStatusResponse:
    """读取配置状态，不暴露配置值和凭据。"""

    return ApiSettingsStatusResponse(configured=service.is_configured())


@router.post("", response_model=ApiSettingsResponse)
def save_api_settings(
    payload: ApiSettingsRequest,
    service: Annotated[ApiSettingsService, Depends(get_api_settings_service)],
) -> ApiSettingsResponse:
    """保存 API 设置，响应不回显任何密钥字段。"""

    try:
        configured = service.save(
            ApiSettings(
                base_url=payload.base_url,
                model=payload.model,
                image_input_enabled=payload.image_input_enabled,
                context_limit=payload.context_limit,
                daily_cost_limit=payload.daily_cost_limit,
            ),
            api_key=payload.api_key,
        )
    except CredentialStoreError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return ApiSettingsResponse(configured=configured)


@router.post("/test", response_model=ApiConnectionTestResponse)
def test_api_connection(
    service: Annotated[ApiSettingsService, Depends(get_api_settings_service)],
) -> ApiConnectionTestResponse:
    """测试远端 API 连接，失败时返回结构化结果而非阻塞本地功能。"""

    try:
        settings = service.load()
    except ValidationError:
        return ApiConnectionTestResponse(
            ok=False,
            error_type="invalid_configuration",
            message="API 配置无效，请改用 HTTPS 或本机 loopback HTTP",
        )
    api_key = service.load_api_key()
    if settings is None or not api_key:
        return ApiConnectionTestResponse(
            ok=False,
            error_type="not_configured",
            message="API 尚未配置",
        )

    try:
        response = send_api_test_request(settings, api_key)
    except httpx.TimeoutException:
        return ApiConnectionTestResponse(
            ok=False,
            error_type="timeout",
            message="连接 API 超时",
        )
    except (httpx.NetworkError, httpx.RequestError):
        return ApiConnectionTestResponse(
            ok=False,
            error_type="network",
            message="无法连接 API 服务",
        )

    if response.status_code in {401, 403}:
        return ApiConnectionTestResponse(
            ok=False,
            error_type="authentication",
            message="API key 验证失败",
        )
    if response.is_error:
        return ApiConnectionTestResponse(
            ok=False,
            error_type="remote",
            message="API 服务返回错误",
        )
    return ApiConnectionTestResponse(ok=True, message="API 连接成功")


def send_api_test_request(settings: ApiSettings, api_key: str) -> httpx.Response:
    """向远端模型 API 发送最小测试请求。"""

    url = f"{str(settings.base_url).rstrip('/')}/chat/completions"
    with httpx.Client(timeout=30.0) as client:
        return client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": settings.model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            },
        )

"""API 设置 HTTP 接口的行为测试。"""

import httpx

from fastapi.testclient import TestClient

from writing_companion.api.settings import send_api_test_request
from writing_companion.app import create_app
from writing_companion.config import Settings
from writing_companion.services.settings import ApiSettings


def make_client(tmp_path, monkeypatch):
    """创建使用临时写作库和内存 keyring 的测试客户端。"""

    credentials: dict[tuple[str, str], str] = {}

    monkeypatch.setattr(
        "writing_companion.services.credentials.keyring.set_password",
        lambda service_name, username, password: credentials.__setitem__(
            (service_name, username),
            password,
        ),
    )
    monkeypatch.setattr(
        "writing_companion.services.credentials.keyring.get_password",
        lambda service_name, username: credentials.get((service_name, username)),
    )
    app = create_app(Settings(library_root=tmp_path))
    return TestClient(app), app, credentials


def test_save_api_settings_keeps_key_out_of_sqlite_and_response(tmp_path, monkeypatch):
    """若 API key 进入 SQLite 或响应体，本测试会失败。"""

    client, app, credentials = make_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/settings/api",
        json={
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-5-mini",
            "image_input_enabled": True,
            "context_limit": 128000,
            "daily_cost_limit": "3.50",
            "api_key": "sk-live-secret",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True
    assert "api_key" not in body
    assert credentials
    with app.state.container["database"].connect() as connection:
        rows = connection.execute("SELECT key, value FROM settings").fetchall()
    raw_dump = repr(rows)
    assert "sk-live-secret" not in raw_dump
    assert ("api.base_url", "https://api.openai.com/v1") in rows
    assert ("api.model", "gpt-5-mini") in rows
    assert ("api.image_input_enabled", "true") in rows
    assert ("api.context_limit", "128000") in rows
    assert ("api.daily_cost_limit", "3.50") in rows


def test_save_api_settings_reports_credential_session_failure(tmp_path, monkeypatch):
    """Windows 凭据会话不可用时，接口必须返回可操作的错误而不是 500。"""

    client, _app, _credentials = make_client(tmp_path, monkeypatch)

    def fail_to_save(_service_name, _username, _api_key):
        raise OSError(1312, "登录会话不存在")

    monkeypatch.setattr(
        "writing_companion.services.credentials.keyring.set_password",
        fail_to_save,
    )

    response = client.post(
        "/api/settings/api",
        json={
            "base_url": "https://api.example.com/v1",
            "model": "writer-model",
            "image_input_enabled": False,
            "context_limit": 32000,
            "daily_cost_limit": "1.00",
            "api_key": "sk-session-test",
        },
    )

    assert response.status_code == 503
    assert "Windows 凭据管理器" in response.json()["detail"]


def test_save_api_settings_without_key_preserves_configured_state(tmp_path, monkeypatch):
    """若未提交 key 时误清空已有凭据状态，本测试会失败。"""

    client, _app, _credentials = make_client(tmp_path, monkeypatch)
    first = client.post(
        "/api/settings/api",
        json={
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-5-mini",
            "image_input_enabled": False,
            "context_limit": 64000,
            "daily_cost_limit": "1.00",
            "api_key": "sk-existing",
        },
    )
    second = client.post(
        "/api/settings/api",
        json={
            "base_url": "https://example.test/v1",
            "model": "local-model",
            "image_input_enabled": True,
            "context_limit": 32000,
            "daily_cost_limit": "0.25",
        },
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == {"configured": True}


def test_save_api_settings_rejects_public_http_base_url(tmp_path, monkeypatch):
    """若公网 HTTP 地址被保存并可能明文发送 Bearer key，本测试会失败。"""

    client, _app, credentials = make_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/settings/api",
        json={
            "base_url": "http://api.example.com/v1",
            "model": "gpt-5-mini",
            "image_input_enabled": True,
            "context_limit": 128000,
            "daily_cost_limit": "3.50",
            "api_key": "sk-public-http",
        },
    )

    assert response.status_code == 422
    assert "公网 API 地址必须使用 HTTPS" in response.text
    assert credentials == {}


def test_save_api_settings_allows_https_base_url(tmp_path, monkeypatch):
    """若 HTTPS API 地址被错误拒绝，本测试会失败。"""

    client, _app, _credentials = make_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/settings/api",
        json={
            "base_url": "https://api.example.com/v1",
            "model": "gpt-5-mini",
            "image_input_enabled": False,
            "context_limit": 64000,
            "daily_cost_limit": "1.00",
            "api_key": "sk-https",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"configured": True}


def test_save_api_settings_allows_loopback_http_for_local_development(tmp_path, monkeypatch):
    """若本机开发 HTTP 地址被错误拒绝，本测试会失败。"""

    client, _app, _credentials = make_client(tmp_path, monkeypatch)

    for base_url in [
        "http://127.0.0.1:11434/v1",
        "http://localhost:11434/v1",
    ]:
        response = client.post(
            "/api/settings/api",
            json={
                "base_url": base_url,
                "model": "local-model",
                "image_input_enabled": False,
                "context_limit": 32000,
                "daily_cost_limit": "0.00",
                "api_key": "sk-local",
            },
        )

        assert response.status_code == 200
        assert response.json() == {"configured": True}


def test_api_connection_test_returns_structured_auth_failure(tmp_path, monkeypatch):
    """若认证失败冒泡为异常或 500，本测试会失败。"""

    client, _app, _credentials = make_client(tmp_path, monkeypatch)
    client.post(
        "/api/settings/api",
        json={
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-5-mini",
            "image_input_enabled": True,
            "context_limit": 128000,
            "daily_cost_limit": "3.50",
            "api_key": "sk-invalid",
        },
    )

    def fake_remote_request(settings, api_key):
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    monkeypatch.setattr(
        "writing_companion.api.settings.send_api_test_request",
        fake_remote_request,
    )

    response = client.post("/api/settings/api/test")

    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "error_type": "authentication",
        "message": "API key 验证失败",
    }


def test_api_connection_test_returns_structured_timeout(tmp_path, monkeypatch):
    """若连接超时阻塞或冒泡为异常，本测试会失败。"""

    client, _app, _credentials = make_client(tmp_path, monkeypatch)
    client.post(
        "/api/settings/api",
        json={
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-5-mini",
            "image_input_enabled": False,
            "context_limit": 32000,
            "daily_cost_limit": "0.50",
            "api_key": "sk-timeout",
        },
    )

    def fake_remote_request(settings, api_key):
        raise httpx.TimeoutException("too slow")

    monkeypatch.setattr(
        "writing_companion.api.settings.send_api_test_request",
        fake_remote_request,
    )

    response = client.post("/api/settings/api/test")

    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "error_type": "timeout",
        "message": "连接 API 超时",
    }


def test_api_connection_test_returns_structured_network_failure(tmp_path, monkeypatch):
    """若网络错误阻断本地功能，本测试会失败。"""

    client, _app, _credentials = make_client(tmp_path, monkeypatch)
    client.post(
        "/api/settings/api",
        json={
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-5-mini",
            "image_input_enabled": False,
            "context_limit": 32000,
            "daily_cost_limit": "0.50",
            "api_key": "sk-network",
        },
    )

    def fake_remote_request(settings, api_key):
        raise httpx.NetworkError("offline")

    monkeypatch.setattr(
        "writing_companion.api.settings.send_api_test_request",
        fake_remote_request,
    )

    response = client.post("/api/settings/api/test")

    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "error_type": "network",
        "message": "无法连接 API 服务",
    }


def test_api_connection_test_handles_legacy_public_http_configuration(
    tmp_path, monkeypatch
):
    """若旧库公网 HTTP 配置导致连接测试 500 或泄露 key，本测试会失败。"""

    client, app, credentials = make_client(tmp_path, monkeypatch)
    credential_store = app.state.container["credential_store"]
    credential_store.save_api_key("sk-legacy-secret")
    with app.state.container["database"].transaction() as connection:
        rows = {
            "api.base_url": "http://api.example.com/v1",
            "api.model": "gpt-5-mini",
            "api.image_input_enabled": "true",
            "api.context_limit": "128000",
            "api.daily_cost_limit": "3.50",
        }
        for key, value in rows.items():
            connection.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?)",
                (key, value),
            )

    response = client.post("/api/settings/api/test")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "ok": False,
        "error_type": "invalid_configuration",
        "message": "API 配置无效，请改用 HTTPS 或本机 loopback HTTP",
    }
    assert "sk-legacy-secret" not in repr(body)
    assert credentials


def test_api_connection_test_success_response_does_not_echo_secret(tmp_path, monkeypatch):
    """若连接测试成功响应回显 API key，本测试会失败。"""

    client, _app, _credentials = make_client(tmp_path, monkeypatch)
    client.post(
        "/api/settings/api",
        json={
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-5-mini",
            "image_input_enabled": True,
            "context_limit": 128000,
            "daily_cost_limit": "3.50",
            "api_key": "sk-success-secret",
        },
    )

    def fake_remote_request(settings, api_key):
        return httpx.Response(200, json={"id": "chatcmpl-test"})

    monkeypatch.setattr(
        "writing_companion.api.settings.send_api_test_request",
        fake_remote_request,
    )

    response = client.post("/api/settings/api/test")

    assert response.status_code == 200
    body = response.json()
    assert body == {"ok": True, "error_type": None, "message": "API 连接成功"}
    assert "sk-success-secret" not in repr(body)


def test_api_connection_probe_allows_slow_model_response(monkeypatch):
    """连接探测应允许首次模型响应超过 5 秒，避免把慢响应误报为超时。"""

    observed = {}

    class FakeClient:
        def __init__(self, timeout):
            observed["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            return httpx.Response(200)

    monkeypatch.setattr("writing_companion.api.settings.httpx.Client", FakeClient)

    response = send_api_test_request(
        ApiSettings(
            base_url="https://api.example.com/v1",
            model="writer-model",
            image_input_enabled=False,
            context_limit=32000,
            daily_cost_limit="1.00",
        ),
        "secret-key",
    )

    assert response.status_code == 200
    assert observed["timeout"] == 30.0


def test_api_settings_status_reports_only_configured_flag(tmp_path, monkeypatch):
    """若配置状态接口泄露秘密或状态错误，本测试会失败。"""

    client, _app, _credentials = make_client(tmp_path, monkeypatch)

    response = client.get("/api/settings/api/status")

    assert response.status_code == 200
    assert response.json() == {"configured": False}
    assert "api_key" not in response.text

    client.post(
        "/api/settings/api",
        json={
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-5-mini",
            "image_input_enabled": True,
            "context_limit": 128000,
            "daily_cost_limit": "3.50",
            "api_key": "sk-status-secret",
        },
    )
    configured = client.get("/api/settings/api/status")

    assert configured.json() == {"configured": True}
    assert "sk-status-secret" not in configured.text

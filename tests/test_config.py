import importlib.util

from writing_companion.config import Settings, load_settings


def test_settings_defaults_to_loopback_mode(tmp_path):
    settings = Settings(library_root=tmp_path)

    assert settings.host == "127.0.0.1"
    assert settings.lan_enabled is False


def test_load_settings_uses_library_root_from_environment(tmp_path):
    settings = load_settings({"WRITING_LIBRARY_ROOT": str(tmp_path)})
    assert settings.library_root == tmp_path
    assert settings.host == "127.0.0.1"
    assert settings.port == 8765


def test_httpx_socks_support_is_installed_for_configured_socks_proxy():
    """配置 SOCKS 代理时，运行环境必须包含 httpx 的 socksio 支持。"""

    assert importlib.util.find_spec("socksio") is not None

"""应用启动和 CLI 入口的聚焦测试。"""

from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from writing_companion.app import create_app
from writing_companion.config import Settings
from writing_companion.services.scheduler import Scheduler
from writing_companion.app import ensure_allowed_host, ensure_loopback_host


def test_startup_creates_one_scheduler_loop_and_runs_initial_report(tmp_path, monkeypatch):
    """应用生命周期只应启动一个后台轮询，并完成一次初始调度。"""

    monkeypatch.setattr(
        "writing_companion.services.credentials.keyring.get_password",
        lambda service_name, username: None,
    )
    app = create_app(Settings(library_root=tmp_path))

    with TestClient(app):
        assert isinstance(app.state.scheduler, Scheduler)
        assert app.state.scheduler_task is not None
        assert not app.state.scheduler_task.done()
        assert app.state.startup_report is not None
        assert app.state.backups is app.state.container["backup_service"]


def test_cli_help_and_rebuild_index_are_exposed(tmp_path):
    """CLI 模块应暴露 serve 和 rebuild-index 两个子命令。"""

    from writing_companion.cli import main

    assert main(["--help"]) == 0
    assert main(["rebuild-index", "--library", str(tmp_path)]) == 0


@pytest.mark.parametrize("host", ["192.168.1.20", "10.0.0.5", "0.0.0.0", "::"])
def test_app_rejects_non_loopback_hosts(host):
    """服务绑定必须拒绝局域网、通配和其他非 loopback 地址。"""

    with pytest.raises(ValueError, match="loopback"):
        ensure_loopback_host(host)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_app_accepts_loopback_hosts(host):
    """服务绑定允许明确的本机 loopback 地址。"""

    ensure_loopback_host(host)


def test_non_loopback_host_requires_explicit_lan_mode():
    """非回环 host 只有显式启用局域网模式时才允许。"""

    with pytest.raises(ValueError, match="loopback"):
        ensure_allowed_host("0.0.0.0", False)

    ensure_allowed_host("0.0.0.0", True)
    ensure_allowed_host("192.168.1.20", True)


def test_cli_lan_mode_binds_all_interfaces_and_prints_mobile_url(
    tmp_path, monkeypatch, capsys
):
    """--lan 应绑定所有接口并输出手机端访问地址。"""

    calls = {}

    def fake_run(app, host, port):
        calls.update(host=host, port=port)

    monkeypatch.setattr("uvicorn.run", fake_run)
    monkeypatch.setattr(
        "writing_companion.cli.discover_private_ipv4_addresses",
        lambda: ["192.168.1.20"],
    )

    from writing_companion.cli import main

    assert main(["serve", "--library", str(tmp_path), "--lan"]) == 0
    assert calls == {"host": "0.0.0.0", "port": 8765}
    assert "http://192.168.1.20:8765/mobile/" in capsys.readouterr().out


def test_discover_private_ipv4_addresses_filters_non_private_and_duplicates(monkeypatch):
    """局域网地址发现只返回去重后的私有 IPv4 地址。"""

    monkeypatch.setattr(
        "writing_companion.cli.socket.getaddrinfo",
        lambda *args: [
            (2, 1, 6, "", ("192.168.1.20", 0)),
            (2, 1, 6, "", ("192.168.1.20", 0)),
            (2, 1, 6, "", ("10.0.0.5", 0)),
            (2, 1, 6, "", ("127.0.0.1", 0)),
            (2, 1, 6, "", ("8.8.8.8", 0)),
        ],
    )

    from writing_companion.cli import discover_private_ipv4_addresses

    assert discover_private_ipv4_addresses() == ["10.0.0.5", "192.168.1.20"]


def test_cli_rejects_non_loopback_host_with_argument_error(tmp_path, monkeypatch):
    """serve 对非 loopback host 应返回参数错误退出码。"""

    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    from writing_companion.cli import main

    assert main(
        ["serve", "--library", str(tmp_path), "--host", "192.168.1.20"]
    ) == 2
    assert main(["serve", "--library", str(tmp_path), "--port", "not-a-port"]) == 2


def test_pyproject_declares_editable_install_and_cli_entrypoint():
    """项目元数据必须支持 src layout 的 editable 安装和 CLI 脚本。"""

    content = Path("pyproject.toml").read_text(encoding="utf-8")

    assert "[build-system]" in content
    assert "build-backend = \"setuptools.build_meta\"" in content
    assert "[project.scripts]" in content
    assert "writing-companion = \"writing_companion.cli:main\"" in content
    assert "[tool.setuptools.packages.find]" in content

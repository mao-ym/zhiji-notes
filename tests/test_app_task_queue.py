from writing_companion.app import create_app
from writing_companion.config import Settings
from writing_companion.services.task_queue import TaskQueue


def test_create_app_registers_local_task_queue(tmp_path, monkeypatch):
    credentials: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        "writing_companion.services.credentials.keyring.get_password",
        lambda service_name, username: credentials.get((service_name, username)),
    )

    app = create_app(Settings(library_root=tmp_path))

    assert isinstance(app.state.container["task_queue"], TaskQueue)


def test_create_app_registers_ai_memory_extractor(tmp_path, monkeypatch):
    """应用容器应为保存后的记忆提取任务注册可用的 AI 提取器。"""

    monkeypatch.setattr(
        "writing_companion.services.credentials.keyring.get_password",
        lambda _service_name, _username: None,
    )

    app = create_app(Settings(library_root=tmp_path))

    assert callable(app.state.container["memory_service"].extractor)

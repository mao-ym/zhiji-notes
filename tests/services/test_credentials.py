"""API 凭据存储的行为测试。"""

from writing_companion.services.credentials import CredentialStore, SERVICE_NAME


def test_credential_store_saves_and_loads_api_key_with_keyring(monkeypatch):
    """若 API key 被写入非 keyring 后端，本测试会失败。"""

    saved_credentials: dict[tuple[str, str], str] = {}

    def fake_set_password(service_name: str, username: str, password: str) -> None:
        saved_credentials[(service_name, username)] = password

    def fake_get_password(service_name: str, username: str) -> str | None:
        return saved_credentials.get((service_name, username))

    monkeypatch.setattr(
        "writing_companion.services.credentials.keyring.set_password",
        fake_set_password,
    )
    monkeypatch.setattr(
        "writing_companion.services.credentials.keyring.get_password",
        fake_get_password,
    )
    store = CredentialStore(library_id="library-one")

    store.save_api_key("sk-secret-value")

    assert store.load_api_key() == "sk-secret-value"
    assert len(saved_credentials) == 1
    (service_name, username), password = next(iter(saved_credentials.items()))
    assert service_name.startswith(f"{SERVICE_NAME}:")
    assert username == "library-one:api_key"
    assert password == "sk-secret-value"


def test_credential_store_uses_library_specific_service_name(monkeypatch):
    """不同资料库保存凭据时不应触发 keyring 的同服务名冲突迁移。"""

    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "writing_companion.services.credentials.keyring.set_password",
        lambda service_name, username, password: calls.append(
            (service_name, username, password)
        ),
    )

    CredentialStore(library_id="library-one").save_api_key("sk-secret-value")

    assert calls
    assert calls[0][0].startswith(f"{SERVICE_NAME}:")
    assert calls[0][0] != SERVICE_NAME


def test_credential_store_reads_legacy_shared_service_name(monkeypatch):
    """升级后仍应读取升级前固定服务名保存的凭据。"""

    def fake_get_password(service_name: str, username: str) -> str | None:
        if service_name == SERVICE_NAME and username == "library-one:api_key":
            return "sk-legacy-value"
        return None

    monkeypatch.setattr(
        "writing_companion.services.credentials.keyring.get_password",
        fake_get_password,
    )

    assert CredentialStore(library_id="library-one").load_api_key() == "sk-legacy-value"

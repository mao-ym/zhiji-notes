"""Windows 凭据管理器中的 API key 存取。"""

from hashlib import sha256
import logging
import os

import keyring


SERVICE_NAME = "Private Writing Companion"
logger = logging.getLogger(__name__)


class CredentialStoreError(RuntimeError):
    """Windows 凭据管理器当前不可用。"""


class CredentialStore:
    """只通过系统凭据后端保存和读取 API key。"""

    def __init__(self, library_id: str) -> None:
        self.library_id = library_id

    def save_api_key(self, api_key: str) -> None:
        """保存 API key，不落盘到写作库数据库。"""

        try:
            keyring.set_password(self._service_name(), self._username(), api_key)
        except Exception as error:
            logger.exception(
                "写入 Windows 凭据失败: service=%s username=%s pid=%s session=%s",
                self._service_name(),
                self._username(),
                os.getpid(),
                os.environ.get("SESSIONNAME"),
            )
            raise CredentialStoreError(
                "无法写入 Windows 凭据管理器，请在交互式 Windows 会话中启动服务后重试"
            ) from error

    def load_api_key(self) -> str | None:
        """读取已保存的 API key。"""

        api_key = keyring.get_password(self._service_name(), self._username())
        if api_key:
            return api_key
        return keyring.get_password(SERVICE_NAME, self._username())

    def _service_name(self) -> str:
        """为每个资料库生成稳定且互不冲突的凭据服务名。"""

        digest = sha256(self.library_id.encode("utf-8")).hexdigest()[:16]
        return f"{SERVICE_NAME}:{digest}"

    def _username(self) -> str:
        """按写作库隔离凭据条目。"""

        return f"{self.library_id}:api_key"

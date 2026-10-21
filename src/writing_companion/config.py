"""应用配置加载。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class Settings:
    """应用运行配置。"""

    library_root: Path
    host: str = "127.0.0.1"
    port: int = 8765
    lan_enabled: bool = False


def load_settings(environ: Mapping[str, str]) -> Settings:
    """从环境变量映射加载应用配置。"""

    root = Path(environ["WRITING_LIBRARY_ROOT"]).expanduser().resolve()
    return Settings(library_root=root)

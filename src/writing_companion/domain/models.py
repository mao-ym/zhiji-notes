"""领域模型定义。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID


DocumentKind = Literal["diary", "fragment", "work"]


@dataclass(frozen=True)
class Document:
    """Markdown 文档的领域对象。"""

    id: UUID
    kind: DocumentKind
    title: str
    body: str
    locked: bool
    updated_at: datetime

"""记忆提取领域结构。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MemoryScope = Literal["global", "document"]
MemoryType = Literal["transient", "long_term"]
MemoryState = Literal["active", "archived", "needs_reconciliation"]


class ChangedRange(BaseModel):
    """文档增量变化片段。"""

    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text: str

    @model_validator(mode="after")
    def reject_reversed_range(self) -> "ChangedRange":
        """拒绝结束位置小于开始位置的非法差异范围。"""

        if self.end < self.start:
            raise ValueError("差异范围结束位置不能小于开始位置")
        return self


class Memory(BaseModel):
    """可入库的结构化记忆。"""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1)
    scope: MemoryScope
    type: MemoryType
    source_ids: list[str] = Field(min_length=1)
    first_seen_at: datetime
    last_seen_at: datetime
    expire_at: datetime | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    state: MemoryState = "active"
    allowed_for_ai: bool = True

    @field_validator("source_ids")
    @classmethod
    def reject_empty_sources(cls, value: list[str]) -> list[str]:
        """拒绝空来源标识，避免无来源记忆进入系统。"""

        if any(not source_id.strip() for source_id in value):
            raise ValueError("记忆来源不能为空")
        return value


class DocumentMemoryPayload(BaseModel):
    """发送给模型的单文档增量 payload。"""

    document_id: str
    version: int = Field(ge=0)
    changed_ranges: list[ChangedRange]
    transient_memories: list[str] = Field(default_factory=list)
    needs_reconciliation: bool = False


class ConversationMessagePayload(BaseModel):
    """用于记忆提取的对话消息，保留完整上下文并标记新增消息。"""

    role: Literal["user", "assistant"]
    content: str
    is_new: bool = False


class ConversationMemoryPayload(BaseModel):
    """从消息游标之后提取记忆的单个对话 payload。"""

    conversation_id: str
    cursor: int = Field(ge=0)
    next_cursor: int = Field(ge=0)
    messages: list[ConversationMessagePayload] = Field(default_factory=list)


class MemoryBatch(BaseModel):
    """一次记忆提取请求的批量 payload。"""

    documents: list[DocumentMemoryPayload]
    conversations: list[ConversationMemoryPayload] = Field(default_factory=list)
    transient_memories: list[str] = Field(default_factory=list)
    long_term_memories: list[str] = Field(default_factory=list)


class MemoryExtractionResponse(BaseModel):
    """模型返回的记忆提取结果。"""

    model_config = ConfigDict(extra="forbid")

    memories: list[Memory] = Field(default_factory=list)


MemoryReviewAction = Literal["promote", "keep", "merge"]


class MemoryReviewDecision(BaseModel):
    """每日回顾器对一条临时记忆给出的生命周期决策。"""

    model_config = ConfigDict(extra="forbid")

    memory_id: str = Field(min_length=1)
    action: MemoryReviewAction
    target_memory_id: str | None = None

    @model_validator(mode="after")
    def require_merge_target(self) -> "MemoryReviewDecision":
        """合并决策必须明确指出已有长期记忆。"""

        if self.action == "merge" and not self.target_memory_id:
            raise ValueError("merge 决策必须包含 target_memory_id")
        return self


class MemoryReviewResponse(BaseModel):
    """每日回顾器返回的结构化决策。"""

    model_config = ConfigDict(extra="forbid")

    decisions: list[MemoryReviewDecision] = Field(default_factory=list)

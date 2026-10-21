"""使用已配置的聊天 API 执行记忆提取和每日回顾。"""

from datetime import datetime, timezone
import json
from typing import Any

from writing_companion.domain.memory_schema import (
    MemoryBatch,
    MemoryReviewResponse,
)
from writing_companion.services.ai_client import OpenAiCompatibleClient


MEMORY_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["memories"],
    "properties": {
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "content",
                    "scope",
                    "source_ids",
                    "first_seen_at",
                    "last_seen_at",
                    "expire_at",
                ],
                "properties": {
                    "content": {"type": "string"},
                    "scope": {"type": "string", "enum": ["global", "document"]},
                    "source_ids": {"type": "array", "items": {"type": "string"}},
                    "first_seen_at": {"type": "string"},
                    "last_seen_at": {"type": "string"},
                    "expire_at": {"type": ["string", "null"]},
                },
            },
        }
    },
}


MEMORY_REVIEW_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["decisions"],
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["memory_id", "action"],
                "properties": {
                    "memory_id": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": ["promote", "keep", "merge"],
                    },
                    "target_memory_id": {"type": "string"},
                },
            },
        }
    },
}


EXTRACTION_PROMPT = """【角色】
你是记忆提取器，负责从文档或对话中识别值得需要保存的记忆信息。

【当前时间】
当前时间：{now}

【输入来源示例】
{example}

【输入说明】
- 你将收到完整的文档 payload，必须阅读其中全部的 changed_ranges。
- 每个 changed_range 可能附带临时记忆（short-term memory）和长期记忆（long-term memory）上下文，提取判断时需结合这些上下文，避免重复保存已存在的记忆，也避免遗漏因上下文变化而需要更新的记忆。
- 来源 ID 规则：
  - 若信息来自文档，source_ids 使用该文档的 ID。
  - 若信息来自对话，source_ids 使用 "conversation:<id>" 格式。

【判断标准】
- 提取对象为：事实、称呼、偏好、背景信息，以及反映用户近期状态（如当前心情、正在进行的事项、临时计划等）的内容。
- 判断依据是内容本身是否值得保存，而非用户是否明确说过“请记住”一类的指令。
- 不要将普通叙述、无实质信息的寒暄强行转换为记忆。
- 已有记忆仅供参考，不能作为新记忆的证据或被直接复制。
- 提取后的记忆生命周期（如是否过期、是否需要合并）由后续的每日记忆回看流程处理，本次任务只负责识别与生成，无需在此环节做归档决策。

【字段规则】
每条记忆对象必须包含以下字段，不可缺失：
- content：值得保存的具体内容，需完整、独立成句，不依赖上下文即可理解。
- scope：仅可为 "global" 或 "document"。
- source_ids：字符串数组，遵循上方来源 ID 规则。
- first_seen_at：该记忆首次出现的时间，ISO 8601 格式。
- last_seen_at：该记忆最近一次出现的时间，ISO 8601 格式。
- expire_at：该记忆预计过期时间，ISO 8601 格式。
  - 反映用户近期状态、临时事项的内容，默认设置为 last_seen_at 之后一个月。
  - 稳定的事实、长期偏好、背景信息等无需过期的内容，可将该字段设为 null。

【输出格式示例】
只能返回一个 JSON 对象，不要返回数组、Markdown 或解释文字。
{
	"memories": [
		{
		  "content": "值得保存的内容",
		  "scope": "global",
		  "source_ids": ["document-id"],
		  "first_seen_at": "2026-01-01T00:00:00+00:00",
		  "last_seen_at": "2026-01-01T00:00:00+00:00",
		  "expire_at": "2026-02-01T00:00:00+00:00"
		}
	]
}
没有可保存内容时返回 {"memories":[]}。
"""


CONTEXT_RULES = """【上下文规则】
- 必须完整阅读本次输入中的所有内容：全部 changed_ranges、所有用户消息和助手消息，以及附带的临时记忆和长期记忆。
- 用完整的用户和助手消息来理解上下文，不能只看用户消息；助手消息用于理解上下文，但不能单独作为新记忆的证据。
- 只有当前输入来源中实际出现且值得保存的新信息才能生成新记忆；已经提取过的内容不要再次提取。
- 附带的临时记忆和长期记忆只用于识别重复、冲突和变化，不能作为新记忆的证据，也不能直接复制。
- 对话中只有新增用户消息（is_new=true 且 role=user）中的新信息可以产生新记忆。
- 文档来源的 source_ids 只能使用当前文档 ID；对话来源的 source_ids 必须使用 conversation:<id> 格式。
"""


REVIEW_PROMPT_TEMPLATE = """【角色】
你是记忆回顾器，负责判断已有临时记忆是否应升级为长期记忆。

【当前时间】
当前时间：{now}

【输入说明】
你将收到：
- 待回顾的临时记忆；
- 已存在的长期记忆，用于判断重复和冲突；
- 每条记忆的来源、首次出现时间、最近出现时间和预计过期时间。
- 临时记忆已经经过基础校验。你只能基于输入内容做生命周期判断，不得创造输入中不存在的新事实。

【判断标准】
- 稳定的事实、称呼、长期偏好、个人背景信息、近期频繁出现的特征，通常应升级为长期记忆。
- 当前心情、正在进行的事项、临时计划、短期提醒等，继续保留为临时记忆。
- 如果临时记忆与已有长期记忆表达的是同一事实，应标记为重复或合并候选。
- 已经过期的记忆由程序处理，不需要在输出中重新判断。
- 不要输出记忆正文之外的新内容。

【输出格式】
只能返回一个 JSON 对象，不要返回数组、Markdown 或解释文字。
{
  "decisions": [
    {"memory_id": "记忆 ID", "action": "promote"},
    {"memory_id": "记忆 ID", "action": "keep"},
    {"memory_id": "记忆 ID", "action": "merge", "target_memory_id": "已有长期记忆 ID"}
  ]
}
"""


class _ConfiguredAi:
    """创建一次按当前配置工作的兼容聊天客户端。"""

    def __init__(self, api_settings) -> None:
        self.api_settings = api_settings

    def client(self) -> OpenAiCompatibleClient:
        settings = self.api_settings.load()
        api_key = self.api_settings.load_api_key()
        if settings is None or not api_key:
            raise RuntimeError("记忆 API 尚未配置")
        return OpenAiCompatibleClient(
            str(settings.base_url),
            api_key,
            settings.model,
            daily_cost_limit=settings.daily_cost_limit,
        )


class ConfiguredMemoryExtractor(_ConfiguredAi):
    """每次执行任务时读取最新配置并调用聊天 API。"""

    def __call__(self, batch: MemoryBatch) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        source_example = "document-id" if batch.documents else "conversation:<id>"
        prompt = (
            EXTRACTION_PROMPT.replace("{example}", source_example)
            .replace("{now}", now)
            + "\n"
            + CONTEXT_RULES
        )
        return self.client().complete(
            messages=[
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": json.dumps(batch.model_dump(mode="json"), ensure_ascii=False),
                },
            ],
            response_schema=MEMORY_RESPONSE_SCHEMA,
        )


class ConfiguredMemoryReviewer(_ConfiguredAi):
    """使用大模型判断临时记忆的生命周期。"""

    def __call__(self, payload: dict[str, Any]) -> MemoryReviewResponse | dict[str, Any]:
        now = str(payload.get("current_time") or datetime.now(timezone.utc).isoformat())
        prompt = REVIEW_PROMPT_TEMPLATE.replace("{now}", now)
        result = self.client().complete(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            response_schema=MEMORY_REVIEW_RESPONSE_SCHEMA,
        )
        return result

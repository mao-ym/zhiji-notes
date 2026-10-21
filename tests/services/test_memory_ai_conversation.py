from types import SimpleNamespace

from writing_companion.domain.memory_schema import MemoryBatch
from writing_companion.services import memory_ai


def test_configured_memory_extractor_includes_full_conversation_context_rules(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def complete(self, messages, response_schema):
            calls.append((messages, response_schema))
            return {"memories": []}

    monkeypatch.setattr(memory_ai, "OpenAiCompatibleClient", FakeClient)
    api_settings = SimpleNamespace(
        load=lambda: SimpleNamespace(
            base_url="https://api.example.test", model="writer-model", daily_cost_limit=0
        ),
        load_api_key=lambda: "secret-key",
    )
    batch = MemoryBatch.model_validate(
        {
            "documents": [],
            "conversations": [
                {
                    "conversation_id": "11111111-1111-4111-8111-111111111111",
                    "cursor": 0,
                    "next_cursor": 2,
                    "messages": [
                        {"role": "user", "content": "我叫毛毛", "is_new": True},
                        {"role": "assistant", "content": "好的，我记住了", "is_new": True},
                    ],
                }
            ],
        }
    )

    memory_ai.ConfiguredMemoryExtractor(api_settings)(batch)

    system_prompt = calls[0][0][0]["content"]
    payload_text = calls[0][0][1]["content"]
    assert "完整的用户和助手消息" in system_prompt
    assert "只有新增用户消息" in system_prompt
    assert "conversation:<id>" in system_prompt
    assert "完整的用户和助手消息来理解上下文" in system_prompt
    assert "明确希望长期记住" not in system_prompt
    assert "transient" not in system_prompt
    assert "long_term" not in system_prompt
    assert "first_seen_at" in system_prompt
    assert "expire_at" in system_prompt
    assert "只能返回一个 JSON 对象" in system_prompt
    assert '{"memories":[' in system_prompt
    assert "不要返回数组" in system_prompt
    assert "以后请叫我X" not in system_prompt
    assert "11111111-1111-4111-8111-111111111111" in payload_text
    assert "好的，我记住了" in payload_text


def test_memory_response_schema_restricts_memory_fields_to_domain_values():
    memory_schema = memory_ai.MEMORY_RESPONSE_SCHEMA["properties"]["memories"]["items"]
    properties = memory_schema["properties"]

    assert properties["scope"]["enum"] == ["global", "document"]
    assert "type" not in memory_schema["required"]
    assert "type" not in properties
    assert "expire_at" in memory_schema["required"]
    assert properties["expire_at"]["type"] == ["string", "null"]
    assert "confidence" not in properties
    assert "state" not in properties
    assert "allowed_for_ai" not in properties


def test_configured_memory_extractor_uses_unified_context_rules_for_document_batches(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def complete(self, messages, response_schema):
            calls.append((messages, response_schema))
            return {"memories": []}

    monkeypatch.setattr(memory_ai, "OpenAiCompatibleClient", FakeClient)
    api_settings = SimpleNamespace(
        load=lambda: SimpleNamespace(
            base_url="https://api.example.test", model="writer-model", daily_cost_limit=0
        ),
        load_api_key=lambda: "secret-key",
    )
    batch = MemoryBatch.model_validate(
        {
            "documents": [
                {
                    "document_id": "22222222-2222-4222-8222-222222222222",
                    "version": 3,
                    "changed_ranges": [
                        {"start": 0, "end": 8, "text": "我喜欢清晨写作"}
                    ],
                    "transient_memories": ["已有临时上下文"],
                    "needs_reconciliation": False,
                }
            ],
            "conversations": [],
        }
    )

    memory_ai.ConfiguredMemoryExtractor(api_settings)(batch)

    system_prompt = calls[0][0][0]["content"]
    payload_text = calls[0][0][1]["content"]
    assert "文档 payload" in system_prompt
    assert "全部 changed_ranges" in system_prompt
    assert "已有记忆仅供参考" in system_prompt
    assert "不能作为新记忆的证据" in system_prompt
    assert "只有新增用户消息" in system_prompt
    assert "完整的用户和助手消息" in system_prompt
    assert "只能返回一个 JSON 对象" in system_prompt
    assert "22222222-2222-4222-8222-222222222222" in payload_text
    assert "我喜欢清晨写作" in payload_text
    assert '"source_ids": ["document-id"]' in system_prompt

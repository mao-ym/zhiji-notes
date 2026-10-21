"""每日记忆回顾 Prompt 和 JSON 决策测试。"""

from types import SimpleNamespace

from writing_companion.domain.memory_schema import MemoryReviewResponse
from writing_companion.services import memory_ai


def test_configured_reviewer_sends_explicit_json_prompt_and_schema(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def complete(self, messages, response_schema):
            calls.append((messages, response_schema))
            return {"decisions": [{"memory_id": "memory-1", "action": "keep"}]}

    monkeypatch.setattr(memory_ai, "OpenAiCompatibleClient", FakeClient)
    settings = SimpleNamespace(
        load=lambda: SimpleNamespace(
            base_url="https://api.example.test", model="gpt-5.6-terra", daily_cost_limit=0
        ),
        load_api_key=lambda: "secret-key",
    )

    result = memory_ai.ConfiguredMemoryReviewer(settings)(
        {
            "current_time": "2026-01-01T00:00:00+00:00",
            "transient_memories": [],
            "long_term_memories": [],
        }
    )

    assert MemoryReviewResponse.model_validate(result).decisions[0].action == "keep"
    prompt = calls[0][0][0]["content"]
    assert "稳定的事实、称呼、长期偏好" in prompt
    assert '"decisions"' in prompt
    assert '"action": "promote"' in prompt
    assert "当前心情、正在进行的事项" in prompt
    assert calls[0][1] is memory_ai.MEMORY_REVIEW_RESPONSE_SCHEMA

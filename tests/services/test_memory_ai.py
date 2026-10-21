from types import SimpleNamespace

from writing_companion.domain.memory_schema import MemoryBatch
from writing_companion.services import memory_ai


def test_configured_memory_extractor_calls_current_api_configuration(monkeypatch):
    """记忆提取器应读取当前 API 配置并把增量批次发送给模型。"""

    calls = []

    class FakeClient:
        def __init__(self, base_url, api_key, model, **kwargs):
            calls.append(("init", base_url, api_key, model, kwargs))

        def complete(self, messages, response_schema):
            calls.append(("complete", messages, response_schema))
            return {"memories": []}

    monkeypatch.setattr(memory_ai, "OpenAiCompatibleClient", FakeClient)
    api_settings = SimpleNamespace(
        load=lambda: SimpleNamespace(
            base_url="https://api.example.test",
            model="writer-model",
            daily_cost_limit=0,
        ),
        load_api_key=lambda: "secret-key",
    )
    batch = MemoryBatch.model_validate(
        {
            "documents": [
                {
                    "document_id": "doc-1",
                    "version": 1,
                    "changed_ranges": [{"start": 0, "end": 4, "text": "记住我叫毛毛"}],
                }
            ]
        }
    )

    result = memory_ai.ConfiguredMemoryExtractor(api_settings)(batch)

    assert result == {"memories": []}
    assert calls[0][:4] == ("init", "https://api.example.test", "secret-key", "writer-model")
    assert calls[1][0] == "complete"
    assert "记住我叫毛毛" in calls[1][1][1]["content"]

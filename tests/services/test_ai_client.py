import json
from decimal import Decimal
from threading import Barrier, Thread

import httpx
import pytest

from writing_companion.services.ai_client import (
    AiResponseSchemaError,
    CostLimitExceeded,
    OpenAiCompatibleClient,
    UsageConfirmationRequired,
)


RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["title", "summary"],
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
    },
}


def test_complete_posts_openai_compatible_json_and_returns_schema_valid_content():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers["Authorization"]
        seen["timeout_header"] = request.headers["Content-Type"]
        seen["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"title": "晨间记录", "summary": "今天写了三页。"},
                                ensure_ascii=False,
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = OpenAiCompatibleClient(
        base_url="https://api.example.test/v1/",
        api_key="secret-key",
        model="writer-model",
        http_client=http_client,
    )

    result = client.complete(
        messages=[{"role": "user", "content": "总结这段日记"}],
        response_schema=RESPONSE_SCHEMA,
    )

    assert result == {"title": "晨间记录", "summary": "今天写了三页。"}
    assert seen["url"] == "https://api.example.test/v1/chat/completions"
    assert seen["authorization"] == "Bearer secret-key"
    assert seen["payload"]["model"] == "writer-model"
    assert seen["payload"]["messages"] == [{"role": "user", "content": "总结这段日记"}]
    assert seen["payload"]["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "writing_companion_response", "schema": RESPONSE_SCHEMA},
    }


def test_stream_text_posts_streaming_request_and_yields_delta_content():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content.decode("utf-8"))
        body = (
            'data: {"choices":[{"delta":{"content":"第一段"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"第二段"}}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":3}}\n\n'
            "data: [DONE]\n\n"
        ).encode("utf-8")
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=body)

    client = OpenAiCompatibleClient(
        base_url="https://api.example.test",
        api_key="secret-key",
        model="writer-model",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert list(client.stream_text([{"role": "user", "content": "继续"}])) == ["第一段", "第二段"]
    assert seen["payload"]["stream"] is True
    assert "response_format" not in seen["payload"]


def test_complete_falls_back_to_json_object_when_schema_mode_is_rejected():
    """兼容接口拒绝 JSON Schema 时，应回退到通用 JSON 模式。"""

    payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        payloads.append(payload)
        if len(payloads) == 1:
            return httpx.Response(422, request=request, json={"error": "unsupported response format"})
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": json.dumps({"title": "标题", "summary": "摘要"})}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    client = OpenAiCompatibleClient(
        base_url="https://api.example.test",
        api_key="secret-key",
        model="writer-model",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.complete(
        messages=[{"role": "user", "content": "总结"}],
        response_schema=RESPONSE_SCHEMA,
    ) == {"title": "标题", "summary": "摘要"}
    assert payloads[1]["response_format"] == {"type": "json_object"}
    assert "JSON" in payloads[1]["messages"][0]["content"]


def test_complete_falls_back_to_plain_completion_when_json_formats_are_rejected():
    """远端同时拒绝两种 JSON 格式时，应退回普通文本请求并继续解析 JSON。"""

    payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content.decode("utf-8")))
        if len(payloads) < 3:
            return httpx.Response(400, request=request, json={"error": "unsupported format"})
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": json.dumps({"title": "标题", "summary": "摘要"})}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    client = OpenAiCompatibleClient(
        base_url="https://api.example.test",
        api_key="secret-key",
        model="writer-model",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.complete(
        messages=[{"role": "user", "content": "总结"}],
        response_schema=RESPONSE_SCHEMA,
    ) == {"title": "标题", "summary": "摘要"}
    assert "response_format" not in payloads[2]


def test_complete_wraps_plain_text_as_reply_for_conversation_schema():
    http_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "What would you like me to help with?"}}
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )
        )
    )
    client = OpenAiCompatibleClient(
        base_url="https://api.example.test",
        api_key="secret-key",
        model="writer-model",
        http_client=http_client,
    )

    assert client.complete(
        messages=[{"role": "user", "content": "hi"}],
        response_schema={
            "type": "object",
            "required": ["reply"],
            "properties": {"reply": {"type": "string"}},
        },
    ) == {"reply": "What would you like me to help with?"}


def test_complete_accepts_reply_for_greeting_schema_with_optional_output_names():
    http_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": json.dumps({"reply": "早上好"})}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )
        )
    )
    client = OpenAiCompatibleClient(
        base_url="https://api.example.test",
        api_key="secret-key",
        model="writer-model",
        http_client=http_client,
    )

    assert client.complete(
        messages=[{"role": "user", "content": "生成欢迎语"}],
        response_schema={
            "type": "object",
            "properties": {
                "greeting": {"type": "string"},
                "reply": {"type": "string"},
            },
        },
    ) == {"reply": "早上好"}


def test_complete_rejects_content_that_does_not_match_json_schema():
    http_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": json.dumps({"title": "缺少摘要"})}}
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )
        )
    )
    client = OpenAiCompatibleClient(
        base_url="https://api.example.test",
        api_key="secret-key",
        model="writer-model",
        http_client=http_client,
    )

    with pytest.raises(AiResponseSchemaError, match="summary"):
        client.complete(
            messages=[{"role": "user", "content": "总结"}],
            response_schema=RESPONSE_SCHEMA,
        )


@pytest.mark.parametrize(
    "content",
    [
        '```json\n{"title": "标题", "summary": "摘要"}\n```',
        [
            {"type": "text", "text": '{"title": "标题", '},
            {"type": "text", "text": '"summary": "摘要"}'},
        ],
    ],
)
def test_complete_parses_json_from_fenced_or_segmented_message_content(content):
    http_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": content}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )
        )
    )
    client = OpenAiCompatibleClient(
        base_url="https://api.example.test",
        api_key="secret-key",
        model="writer-model",
        http_client=http_client,
    )

    assert client.complete(
        messages=[{"role": "user", "content": "总结"}],
        response_schema=RESPONSE_SCHEMA,
    ) == {"title": "标题", "summary": "摘要"}


def test_complete_enforces_enum_and_numeric_bounds_from_json_schema():
    http_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {"state": "unknown", "confidence": 1.5}
                                )
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )
        )
    )
    client = OpenAiCompatibleClient(
        base_url="https://api.example.test",
        api_key="secret-key",
        model="writer-model",
        http_client=http_client,
    )

    with pytest.raises(AiResponseSchemaError, match="state"):
        client.complete(
            messages=[{"role": "user", "content": "提取记忆"}],
            response_schema={
                "type": "object",
                "required": ["state", "confidence"],
                "properties": {
                    "state": {"type": "string", "enum": ["active", "archived"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        )


def test_complete_raises_when_usage_is_required_but_missing():
    http_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {"title": "晨间记录", "summary": "今天写了三页。"},
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                },
            )
        )
    )
    client = OpenAiCompatibleClient(
        base_url="https://api.example.test",
        api_key="secret-key",
        model="writer-model",
        http_client=http_client,
        require_usage_confirmation=True,
    )

    with pytest.raises(UsageConfirmationRequired):
        client.complete(
            messages=[{"role": "user", "content": "总结"}],
            response_schema=RESPONSE_SCHEMA,
        )


def test_complete_raises_before_returning_when_daily_cost_limit_would_be_exceeded():
    http_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {"title": "晨间记录", "summary": "今天写了三页。"},
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 1000},
                },
            )
        )
    )
    client = OpenAiCompatibleClient(
        base_url="https://api.example.test",
        api_key="secret-key",
        model="writer-model",
        http_client=http_client,
        daily_cost_limit=Decimal("0.01"),
        daily_cost_spent=Decimal("0.009"),
        input_token_price=Decimal("0.01"),
        output_token_price=Decimal("0.03"),
    )

    with pytest.raises(CostLimitExceeded):
        client.complete(
            messages=[{"role": "user", "content": "总结"}],
            response_schema=RESPONSE_SCHEMA,
        )

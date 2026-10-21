import json
from decimal import Decimal
from threading import Barrier, Thread

import httpx
import pytest

from writing_companion.services.ai_client import (
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


def successful_response(usage):
    return {
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
        "usage": usage,
    }


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {"prompt_tokens": 1},
        {"completion_tokens": 1},
        {"prompt_tokens": "not-a-number", "completion_tokens": 1},
        {"prompt_tokens": 1, "completion_tokens": None},
        [],
    ],
)
def test_complete_requires_mapping_usage_with_valid_prompt_and_completion_tokens(usage):
    http_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=successful_response(usage))
        )
    )
    client = OpenAiCompatibleClient(
        base_url="https://api.example.test",
        api_key="secret-key",
        model="writer-model",
        http_client=http_client,
    )

    with pytest.raises(UsageConfirmationRequired):
        client.complete(
            messages=[{"role": "user", "content": "总结"}],
            response_schema=RESPONSE_SCHEMA,
        )


def test_complete_accumulates_daily_cost_after_success():
    http_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json=successful_response(
                    {"prompt_tokens": 1000, "completion_tokens": 1000}
                ),
            )
        )
    )
    client = OpenAiCompatibleClient(
        base_url="https://api.example.test",
        api_key="secret-key",
        model="writer-model",
        http_client=http_client,
        daily_cost_limit=Decimal("0.10"),
        daily_cost_spent=Decimal("0"),
        input_token_price=Decimal("0.01"),
        output_token_price=Decimal("0.03"),
    )

    client.complete(
        messages=[{"role": "user", "content": "总结"}],
        response_schema=RESPONSE_SCHEMA,
    )
    client.complete(
        messages=[{"role": "user", "content": "总结"}],
        response_schema=RESPONSE_SCHEMA,
    )

    assert client.daily_cost_spent == Decimal("0.08")


def test_complete_reserves_daily_cost_atomically_for_concurrent_calls():
    barrier = Barrier(2)

    def handler(_request: httpx.Request) -> httpx.Response:
        barrier.wait(timeout=5)
        return httpx.Response(
            200,
            json=successful_response({"prompt_tokens": 1000, "completion_tokens": 1000}),
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = OpenAiCompatibleClient(
        base_url="https://api.example.test",
        api_key="secret-key",
        model="writer-model",
        http_client=http_client,
        daily_cost_limit=Decimal("0.04"),
        daily_cost_spent=Decimal("0"),
        input_token_price=Decimal("0.01"),
        output_token_price=Decimal("0.03"),
    )
    results = []
    errors = []

    def call_client():
        try:
            results.append(
                client.complete(
                    messages=[{"role": "user", "content": "总结"}],
                    response_schema=RESPONSE_SCHEMA,
                )
            )
        except CostLimitExceeded as error:
            errors.append(error)

    threads = [Thread(target=call_client), Thread(target=call_client)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(results) == 1
    assert len(errors) == 1
    assert client.daily_cost_spent == Decimal("0.04")

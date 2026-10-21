"""OpenAI 兼容聊天补全客户端。"""

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
import json
from threading import Lock
from typing import Any

import httpx


class AiClientError(RuntimeError):
    """AI 客户端基础异常。"""


class AiResponseSchemaError(AiClientError):
    """模型返回内容不符合调用方要求的 JSON Schema。"""


class CostLimitExceeded(AiClientError):
    """本次调用会超过每日费用上限。"""


class UsageConfirmationRequired(AiClientError):
    """响应缺少 usage，无法确认费用。"""


class OpenAiCompatibleClient:
    """调用 `{base_url}/chat/completions` 的 OpenAI 兼容客户端。"""

    STREAM_READ_TIMEOUT_SECONDS = 120.0

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        http_client: httpx.Client | None = None,
        daily_cost_limit: Decimal | None = None,
        daily_cost_spent: Decimal = Decimal("0"),
        input_token_price: Decimal = Decimal("0"),
        output_token_price: Decimal = Decimal("0"),
        require_usage_confirmation: bool = False,
        context_limit: int | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.daily_cost_limit = daily_cost_limit
        self.daily_cost_spent = daily_cost_spent
        self.input_token_price = input_token_price
        self.output_token_price = output_token_price
        self.require_usage_confirmation = require_usage_confirmation
        self.context_limit = context_limit
        self._cost_lock = Lock()
        self.http_client = http_client or httpx.Client(
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
        )

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        response_schema: Mapping[str, Any],
    ) -> dict[str, Any]:
        """请求聊天补全，返回通过 JSON Schema 校验的对象。"""

        try:
            response = self._post_completion(
                messages,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "writing_companion_response",
                        "schema": dict(response_schema),
                    },
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            if error.response.status_code not in (400, 422):
                raise
            fallback_messages = [
                {
                    "role": "system",
                    "content": (
                        "请只返回一个合法的 JSON 对象，不要使用 Markdown。"
                        "返回对象必须符合以下结构："
                        + json.dumps(dict(response_schema), ensure_ascii=False)
                    ),
                },
                *list(messages),
            ]
            try:
                response = self._post_completion(
                    fallback_messages,
                    response_format={"type": "json_object"},
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as fallback_error:
                if fallback_error.response.status_code not in (400, 422):
                    raise
                response = self._post_completion(fallback_messages, response_format=None)
                response.raise_for_status()
        body = response.json()
        try:
            result = _extract_json_content(body)
        except AiResponseSchemaError:
            content = _extract_message_content(body)
            if not _supports_plain_reply(response_schema) or not isinstance(content, str):
                raise
            result = {"reply": content.strip()}
        _validate_schema(result, response_schema, path="$")
        self._record_usage_cost(body.get("usage"))
        return result

    def stream_text(self, messages: Sequence[Mapping[str, Any]]):
        """以 OpenAI SSE 格式逐段返回聊天文本。"""

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        usage = None
        try:
            with self.http_client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=httpx.Timeout(
                    connect=5.0,
                    read=self.STREAM_READ_TIMEOUT_SECONDS,
                    write=10.0,
                    pool=5.0,
                ),
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError as error:
                        raise AiResponseSchemaError("流式聊天响应不是有效 JSON") from error
                    usage = chunk.get("usage") or usage
                    choices = chunk.get("choices") or []
                    if not choices or not isinstance(choices[0], Mapping):
                        continue
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content") if isinstance(delta, Mapping) else None
                    if isinstance(content, str) and content:
                        yield content
        except httpx.HTTPStatusError:
            raise
        except httpx.RequestError:
            raise
        self._record_usage_cost(usage)

    def _post_completion(
        self,
        messages: Sequence[Mapping[str, Any]],
        response_format: Mapping[str, Any] | None,
    ) -> httpx.Response:
        """发送一次聊天请求，供结构化格式和兼容格式共用。"""

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
        }
        if response_format is not None:
            payload["response_format"] = dict(response_format)
        return self.http_client.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    def _record_usage_cost(self, usage: Any) -> None:
        """校验 usage 并以原子方式累计每日费用。"""

        prompt_tokens, completion_tokens = _parse_usage_tokens(usage)
        estimated_cost = (
            prompt_tokens / Decimal("1000") * self.input_token_price
            + completion_tokens / Decimal("1000") * self.output_token_price
        )
        with self._cost_lock:
            if (
                self.daily_cost_limit is not None
                and self.daily_cost_spent + estimated_cost > self.daily_cost_limit
            ):
                raise CostLimitExceeded("本次调用会超过每日费用上限")
            self.daily_cost_spent += estimated_cost


def _extract_message_content(body: Mapping[str, Any]) -> Any:
    """提取 OpenAI 兼容响应中的 message.content。"""

    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise AiResponseSchemaError("响应缺少 choices[0].message.content") from error


def _supports_plain_reply(schema: Mapping[str, Any]) -> bool:
    """判断当前结构化协议是否允许把普通文本包装成 reply。"""

    properties = schema.get("properties", {})
    return (
        schema.get("type") == "object"
        and properties.get("reply", {}).get("type") == "string"
        and (
            "reply" in schema.get("required", [])
            or "greeting" in properties
        )
    )


def _extract_json_content(body: Mapping[str, Any]) -> dict[str, Any]:
    """从 OpenAI 兼容响应中解析 message.content 的 JSON 对象。"""

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise AiResponseSchemaError("响应缺少 choices[0].message.content") from error
    if isinstance(content, dict):
        return content
    if isinstance(content, list):
        text_parts = [
            part.get("text")
            for part in content
            if isinstance(part, Mapping) and isinstance(part.get("text"), str)
        ]
        if text_parts:
            content = "".join(text_parts)
    if isinstance(content, str):
        content = content.strip()
        if content.startswith("```") and content.endswith("```"):
            lines = content.splitlines()
            if len(lines) >= 3:
                content = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(content)
    except (TypeError, json.JSONDecodeError) as error:
        raise AiResponseSchemaError("模型返回内容不是 JSON") from error
    if not isinstance(parsed, dict):
        raise AiResponseSchemaError("模型返回 JSON 必须是对象")
    return parsed


def _validate_schema(value: Any, schema: Mapping[str, Any], path: str) -> None:
    """校验本项目需要的 JSON Schema 子集。"""

    if "enum" in schema and value not in schema["enum"]:
        raise AiResponseSchemaError(f"{path} 不在允许值范围内")
    expected_type = schema.get("type")
    if isinstance(expected_type, list):
        if value is None and "null" in expected_type:
            return
        if not any(_matches_json_type(value, item) for item in expected_type if item != "null"):
            raise AiResponseSchemaError(f"{path} 类型不符合要求")
        return
    if expected_type == "object":
        if not isinstance(value, dict):
            raise AiResponseSchemaError(f"{path} 必须是对象")
        for key in schema.get("required", []):
            if key not in value:
                raise AiResponseSchemaError(f"{path}.{key} 缺失")
        properties = schema.get("properties", {})
        for key, property_schema in properties.items():
            if key in value:
                _validate_schema(value[key], property_schema, f"{path}.{key}")
        return
    if expected_type == "string" and not isinstance(value, str):
        raise AiResponseSchemaError(f"{path} 必须是字符串")
    if expected_type == "number":
        if not _is_number(value):
            raise AiResponseSchemaError(f"{path} 必须是数字")
        if "minimum" in schema and value < schema["minimum"]:
            raise AiResponseSchemaError(f"{path} 小于最小值")
        if "maximum" in schema and value > schema["maximum"]:
            raise AiResponseSchemaError(f"{path} 大于最大值")
    if expected_type == "integer" and not (
        isinstance(value, int) and not isinstance(value, bool)
    ):
        raise AiResponseSchemaError(f"{path} 必须是整数")
    if expected_type == "boolean" and not isinstance(value, bool):
        raise AiResponseSchemaError(f"{path} 必须是布尔值")
    if expected_type == "array":
        if not isinstance(value, list):
            raise AiResponseSchemaError(f"{path} 必须是数组")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                _validate_schema(item, item_schema, f"{path}[{index}]")


def _is_number(value: Any) -> bool:
    """排除 bool 的 JSON number 判断。"""

    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _matches_json_type(value: Any, expected_type: str) -> bool:
    """判断值是否符合 JSON Schema 的基础类型。"""

    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "number": _is_number(value),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected_type, True)


def _parse_usage_tokens(usage: Any) -> tuple[Decimal, Decimal]:
    """严格解析 usage，缺失或非法都需要人工费用确认。"""

    if not isinstance(usage, Mapping):
        raise UsageConfirmationRequired("响应 usage 缺失或格式无效，无法确认费用")
    if "prompt_tokens" not in usage or "completion_tokens" not in usage:
        raise UsageConfirmationRequired("响应 usage 缺少 token 字段，无法确认费用")
    return (
        _parse_token_count(usage["prompt_tokens"], "prompt_tokens"),
        _parse_token_count(usage["completion_tokens"], "completion_tokens"),
    )


def _parse_token_count(value: Any, field_name: str) -> Decimal:
    """解析非负 token 数量。"""

    if isinstance(value, bool):
        raise UsageConfirmationRequired(f"usage.{field_name} 不是有效数字")
    try:
        token_count = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise UsageConfirmationRequired(f"usage.{field_name} 不是有效数字") from None
    if not token_count.is_finite() or token_count < 0:
        raise UsageConfirmationRequired(f"usage.{field_name} 不是有效数字")
    return token_count

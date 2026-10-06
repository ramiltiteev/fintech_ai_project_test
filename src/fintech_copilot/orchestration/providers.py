from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import TypeAdapter

from fintech_copilot.domain.results import ToolResult
from fintech_copilot.tools.dispatcher import TOOLS

from .grounding import full_results, mock_synthesis


class ConfigurationError(ValueError):
    pass


class ProviderFailure(Exception):
    def __init__(self, code: str, transient: bool = False) -> None:
        super().__init__(code)
        self.code, self.transient = code, transient


@dataclass(frozen=True)
class Call:
    id: str
    name: str
    arguments: str

    def wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass(frozen=True)
class Turn:
    calls: tuple[Call, ...] = ()
    content: str | None = None
    assistant_message: dict[str, Any] | None = None

    def message(self) -> dict[str, Any]:
        if self.assistant_message is not None:
            return deepcopy(self.assistant_message)
        result: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.calls:
            result["tool_calls"] = [call.wire() for call in self.calls]
        return result


class Provider(Protocol):
    mode: Literal["mock", "live"]
    model: str | None

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], timeout: float
    ) -> Turn: ...


class MockProvider:
    mode: Literal["mock", "live"] = "mock"
    model: str | None = None

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], timeout: float
    ) -> Turn:
        from fintech_copilot.domain.models import Scope

        context = json.loads(messages[1]["content"])
        scope = Scope.model_validate(context["scope"])
        results: list[ToolResult] = []
        for message in messages:
            if message["role"] == "tool":
                raw = json.loads(message["content"])
                if "tool_result_id" in raw:
                    results.append(TypeAdapter(ToolResult).validate_python(raw))
        successful = full_results(tuple(results), scope)
        names = {r.tool_name for r in successful}
        # Scripted provider turns still consume actual dispatcher/tool messages.
        for group in ((TOOLS[0],), (TOOLS[1], TOOLS[2]), (TOOLS[3],)):
            missing = tuple(name for name in group if name not in names)
            if missing:
                index = sum(message["role"] == "assistant" for message in messages)
                return Turn(
                    calls=tuple(
                        Call(f"mock-{index}-{n}", name, scope.model_dump_json())
                        for n, name in enumerate(missing)
                    )
                )
        return Turn(content=mock_synthesis(successful).model_dump_json())


class LiveProvider:
    mode: Literal["mock", "live"] = "live"

    def __init__(self) -> None:
        self.model = os.environ.get("LLM_MODEL")
        if not self.model:
            raise ConfigurationError("Live mode requires LLM_MODEL")
        prefix = self.model.split("/", 1)[0]
        key_env = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "gemini": "GEMINI_API_KEY",
        }.get(prefix)
        if not key_env:
            raise ConfigurationError("Use an explicit openai/, anthropic/ or gemini/ model prefix")
        if not os.environ.get(key_env):
            raise ConfigurationError(f"Live mode requires {key_env}")
        try:
            import litellm
        except ImportError as exc:
            raise ConfigurationError("Install the optional live dependency") from exc
        litellm.set_verbose = False
        self.client = litellm

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], timeout: float
    ) -> Turn:
        try:
            response = await self.client.acompletion(
                model=self.model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                timeout=timeout,
                max_retries=0,
                temperature=0,
            )
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status in {401, 403}:
                raise ProviderFailure("AUTH_ERROR") from None
            transient = (
                status in {408, 429, 500, 502, 503, 504}
                or isinstance(exc, (TimeoutError, ConnectionError))
                or type(exc).__name__ in {"Timeout", "APIConnectionError", "RateLimitError"}
            )
            raise ProviderFailure("PROVIDER_ERROR", transient=transient) from None
        try:
            wire = response.model_dump() if hasattr(response, "model_dump") else response
            message = wire["choices"][0]["message"]
            calls = tuple(
                Call(call["id"], call["function"]["name"], call["function"]["arguments"])
                for call in message.get("tool_calls") or []
            )
            if any(
                not all(isinstance(value, str) for value in (c.id, c.name, c.arguments))
                for c in calls
            ):
                raise ValueError("Invalid call")
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                raise ValueError("Invalid content")
            if message.get("role") != "assistant":
                raise ValueError("Expected assistant role")
            return Turn(calls=calls, content=content, assistant_message=message)
        except (ValueError, KeyError, TypeError, IndexError):
            raise ProviderFailure("INVALID_PROVIDER_RESPONSE") from None

from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from fintech_copilot.tools.dispatcher import TOOLS

from .context import SYNTHESIS_OPTIONS_PREFIX
from .models import Insight, Synthesis


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
class TokenUsage:
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    reasoning_tokens: int | None

    @classmethod
    def from_wire(cls, raw: Any) -> TokenUsage | None:
        if not isinstance(raw, dict):
            return None
        details = raw.get("completion_tokens_details")
        details = details if isinstance(details, dict) else {}

        def count(value: Any) -> int | None:
            return value if type(value) is int and value >= 0 else None

        return cls(
            prompt_tokens=count(raw.get("prompt_tokens")),
            completion_tokens=count(raw.get("completion_tokens")),
            total_tokens=count(raw.get("total_tokens")),
            reasoning_tokens=count(details.get("reasoning_tokens")),
        )


@dataclass(frozen=True)
class Turn:
    calls: tuple[Call, ...] = ()
    content: str | None = None
    assistant_message: dict[str, Any] | None = None
    finish_reason: str | None = None
    usage: TokenUsage | None = None

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
        names: set[str] = set()
        guidance: dict[str, Any] | None = None
        for message in messages:
            if message["role"] == "tool":
                raw = json.loads(message["content"])
                if (
                    raw.get("status") == "ok"
                    and "tool_result_id" in raw
                    and all(raw.get(key) == value for key, value in context["scope"].items())
                ):
                    names.add(raw["tool_name"])
            elif message["role"] == "user" and message["content"].startswith(
                SYNTHESIS_OPTIONS_PREFIX
            ):
                guidance = json.loads(message["content"].split("\n")[1])
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
        if guidance is None:
            raise ProviderFailure("MISSING_SYNTHESIS_OPTIONS")
        synthesis = Synthesis(
            insights=tuple(
                Insight(
                    finding_code=option["finding_code"],
                    action_code=option["allowed_actions"][0],
                    evidence=option["evidence_bundles"][0],
                )
                for option in guidance["finding_options"][:6]
            )
        )
        return Turn(content=synthesis.model_dump_json())


class LiveProvider:
    mode: Literal["mock", "live"] = "live"

    def __init__(self) -> None:
        self.model: str | None = os.environ.get("LLM_MODEL", "").strip()
        if not self.model:
            raise ConfigurationError("Live mode requires LLM_MODEL")
        prefix, separator, alias = self.model.partition("/")
        if not separator or not alias.strip() or any(char.isspace() for char in self.model):
            raise ConfigurationError("LLM_MODEL requires a provider prefix and nonempty model name")
        self._gateway_options: dict[str, str] = {}
        if prefix == "litellm_proxy":
            address = os.environ.get("LITELLM_PROXY_API_BASE", "").strip()
            if not address:
                raise ConfigurationError("Gateway mode requires LITELLM_PROXY_API_BASE")
            try:
                parts = urlsplit(address)
                _ = parts.port  # Validate a supplied port as well as the hostname.
                valid_url = (
                    parts.scheme in {"http", "https"}
                    and bool(parts.hostname)
                    and parts.username is None
                    and parts.password is None
                    and not parts.query
                    and not parts.fragment
                    and not any(char.isspace() for char in address)
                )
            except ValueError:
                valid_url = False
            if not valid_url:
                raise ConfigurationError(
                    "LITELLM_PROXY_API_BASE must be an HTTP(S) URL "
                    "without credentials, query or fragment"
                )
            key = os.environ.get("LITELLM_PROXY_API_KEY", "").strip()
            if not key:
                raise ConfigurationError("Gateway mode requires LITELLM_PROXY_API_KEY")
            self._gateway_options = {"api_base": address.rstrip("/"), "api_key": key}
        else:
            key_env = {
                "openai": "OPENAI_API_KEY",
                "anthropic": "ANTHROPIC_API_KEY",
                "gemini": "GEMINI_API_KEY",
            }.get(prefix)
            if not key_env:
                raise ConfigurationError(
                    "Use a litellm_proxy/, openai/, anthropic/ or gemini/ model prefix"
                )
            if not os.environ.get(key_env, "").strip():
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
        # JSON mode is used only after the server has all required tool evidence.
        phase_options: dict[str, Any] = (
            {"tools": tools, "tool_choice": "auto"}
            if tools
            else {"response_format": {"type": "json_object"}}
        )
        try:
            response = await self.client.acompletion(
                model=self.model,
                messages=messages,
                timeout=timeout,
                max_retries=0,
                temperature=0,
                **phase_options,
                **self._gateway_options,
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
            choice = wire["choices"][0]
            message = choice["message"]
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
            finish_reason = choice.get("finish_reason")
            if finish_reason is not None and (
                not isinstance(finish_reason, str)
                or finish_reason
                not in {
                    "stop",
                    "length",
                    "tool_calls",
                    "function_call",
                    "content_filter",
                    "repetition_truncation",
                    "error",
                    "refusal",
                }
            ):
                finish_reason = "unknown"
            return Turn(
                calls=calls,
                content=content,
                assistant_message=message,
                finish_reason=finish_reason,
                usage=TokenUsage.from_wire(wire.get("usage")),
            )
        except (ValueError, KeyError, TypeError, IndexError):
            raise ProviderFailure("INVALID_PROVIDER_RESPONSE") from None

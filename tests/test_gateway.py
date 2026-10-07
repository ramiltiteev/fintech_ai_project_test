"""Offline transport checks for Gateway routing; no server or real key is used."""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from fintech_copilot.cli import main
from fintech_copilot.orchestration.agent import run_agent
from fintech_copilot.orchestration.providers import ConfigurationError, LiveProvider, MockProvider

GATEWAY_KEY = "synthetic-gateway-key"
GATEWAY_MODEL = "litellm_proxy/financial-analysis"


@pytest.fixture
def gateway_env(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", GATEWAY_MODEL)
    monkeypatch.setenv("LITELLM_PROXY_API_BASE", "http://gateway.test:4000/")
    monkeypatch.setenv("LITELLM_PROXY_API_KEY", GATEWAY_KEY)
    for variable in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace())


@pytest.mark.parametrize("cid", ["apex-logistics", "cloudscale-ai", "harbour-trade"])
def test_gateway_complete_agent_with_virtual_key(gateway_env, monkeypatch, golden, validator, cid):
    requests = []
    mock = MockProvider()

    async def acompletion(**kwargs):
        requests.append(kwargs)
        turn = await mock.complete(kwargs["messages"], kwargs.get("tools", []), kwargs["timeout"])
        message = turn.message()
        message["reasoning_content"] = "Synthetic provider field preserved across turns"
        return {"choices": [{"message": message}]}

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=acompletion))
    output = asyncio.run(run_agent(golden[cid], "Synthetic question", LiveProvider()))
    assert output.report.status == "complete"
    assert output.report.provider_mode == "live" and output.report.model == GATEWAY_MODEL
    validator("report.schema.json").validate(output.report.model_dump(mode="json"))
    assert len(requests) == 4
    assert all(request["model"] == GATEWAY_MODEL for request in requests)
    assert all(request["api_base"] == "http://gateway.test:4000" for request in requests)
    assert all(request["api_key"] == GATEWAY_KEY for request in requests)
    assert all(request["max_retries"] == 0 for request in requests)
    assert all(request["tool_choice"] == "auto" for request in requests[:-1])
    assert all("response_format" not in request for request in requests[:-1])
    assert requests[-1]["response_format"] == {"type": "json_object"}
    assert "tools" not in requests[-1] and "tool_choice" not in requests[-1]
    assistants = [m for m in output.messages if m["role"] == "assistant"]
    assert all("reasoning_content" in message for message in assistants)
    assert GATEWAY_KEY not in output.report.model_dump_json()
    assert GATEWAY_KEY not in json.dumps(output.trace)
    assert GATEWAY_KEY not in json.dumps(output.messages)
    assert "gateway.test" not in json.dumps(output.trace)


@pytest.mark.parametrize("variable", ["LITELLM_PROXY_API_BASE", "LITELLM_PROXY_API_KEY"])
def test_gateway_missing_settings_do_not_fallback(gateway_env, monkeypatch, variable):
    monkeypatch.delenv(variable)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-upstream-key")
    with pytest.raises(ConfigurationError, match=variable):
        LiveProvider()


@pytest.mark.parametrize(
    "model", ["litellm_proxy/", "litellm_proxy/ ", "litellm_proxy", "litellm_proxy/bad alias"]
)
def test_gateway_requires_model_alias(gateway_env, monkeypatch, model):
    monkeypatch.setenv("LLM_MODEL", model)
    with pytest.raises(ConfigurationError, match="model name"):
        LiveProvider()


@pytest.mark.parametrize(
    "address",
    [
        "gateway.test:4000",
        "ftp://gateway.test",
        "https://",
        "https://gateway.test:invalid",
        "https://user:synthetic-password@gateway.test",
        "https://gateway.test?token=synthetic-token",
        "https://gateway.test#fragment",
        "http://gate way.test",
        "http://[broken",
    ],
)
def test_gateway_rejects_invalid_or_secret_bearing_urls(gateway_env, monkeypatch, address):
    monkeypatch.setenv("LITELLM_PROXY_API_BASE", address)
    with pytest.raises(ConfigurationError, match="HTTP") as error:
        LiveProvider()
    assert address not in str(error.value)


@pytest.mark.parametrize(
    "address,expected",
    [
        ("http://localhost:4000/", "http://localhost:4000"),
        ("https://gateway.test/v1/", "https://gateway.test/v1"),
        ("https://gateway.test/llm/v1/", "https://gateway.test/llm/v1"),
    ],
)
def test_gateway_url_preserves_api_prefix(gateway_env, monkeypatch, address, expected):
    requests = []

    async def acompletion(**kwargs):
        requests.append(kwargs)
        return {"choices": [{"message": {"role": "assistant", "content": "{}"}}]}

    monkeypatch.setenv("LITELLM_PROXY_API_BASE", address)
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=acompletion))
    asyncio.run(LiveProvider().complete([], [], 5))
    assert requests[0]["api_base"] == expected


def test_gateway_auth_error_is_safe_and_not_retried(gateway_env, monkeypatch, golden):
    calls = 0

    class AuthenticationFailure(Exception):
        status_code = 401

    async def acompletion(**kwargs):
        nonlocal calls
        calls += 1
        raise AuthenticationFailure(f"https://gateway.test rejected {GATEWAY_KEY}")

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=acompletion))
    output = asyncio.run(run_agent(golden["cloudscale-ai"], "Synthetic question", LiveProvider()))
    assert calls == 1
    assert output.report.status == "partial" and "AUTH_ERROR" in output.report.partial_reasons
    assert GATEWAY_KEY not in output.report.model_dump_json() + json.dumps(output.trace)


def test_gateway_configuration_error_before_output(gateway_env, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("LITELLM_PROXY_API_KEY")
    output = tmp_path / "uncreated"
    assert main(["demo", "--provider", "live", "--output", str(output)]) == 2
    assert not output.exists()
    assert "LITELLM_PROXY_API_KEY" in capsys.readouterr().err


def test_mock_ignores_gateway_configuration(gateway_env, monkeypatch, tmp_path):
    monkeypatch.setenv("LITELLM_PROXY_API_BASE", "invalid URL")
    monkeypatch.delenv("LITELLM_PROXY_API_KEY")
    assert main(["demo", "--company", "cloudscale-ai", "--output", str(tmp_path)]) == 0
    report = json.loads((tmp_path / "cloudscale-ai/report.json").read_text())
    assert report["provider_mode"] == "mock" and report["model"] is None


def test_direct_provider_ignores_gateway_options(gateway_env, monkeypatch):
    requests = []

    async def acompletion(**kwargs):
        requests.append(kwargs)
        return {"choices": [{"message": {"role": "assistant", "content": "{}"}}]}

    monkeypatch.setenv("LLM_MODEL", "openai/synthetic-model")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-upstream-key")
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=acompletion))
    asyncio.run(LiveProvider().complete([], [], 5))
    assert requests[0]["model"] == "openai/synthetic-model"
    assert "api_base" not in requests[0] and "api_key" not in requests[0]

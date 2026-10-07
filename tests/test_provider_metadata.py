"""Offline transport fixtures verify token accounting survives into persisted trace."""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from fintech_copilot.cli import main
from fintech_copilot.orchestration.providers import LiveProvider, MockProvider

SECRET = "synthetic-response-secret"


@pytest.fixture
def live_env(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "litellm_proxy/synthetic-model")
    monkeypatch.setenv("LITELLM_PROXY_API_BASE", "https://gateway.test/v1")
    monkeypatch.setenv("LITELLM_PROXY_API_KEY", SECRET)


@pytest.mark.parametrize("finish_reason", ["stop", "length"])
def test_sdk_usage_and_finish_reason_reach_cli_trace(
    live_env, monkeypatch, tmp_path, finish_reason
):
    mock = MockProvider()

    async def acompletion(**kwargs):
        assert "max_tokens" not in kwargs and "max_completion_tokens" not in kwargs
        turn = await mock.complete(kwargs["messages"], kwargs.get("tools", []), kwargs["timeout"])
        message = turn.message()
        message["reasoning_content"] = SECRET
        wire = {
            "choices": [
                {"message": message, "finish_reason": "tool_calls" if turn.calls else finish_reason}
            ],
            "usage": {
                "prompt_tokens": 1200,
                "completion_tokens": 4096,
                "total_tokens": 5296,
                "completion_tokens_details": {"reasoning_tokens": 3800, "other_metadata": SECRET},
                "extra_metadata": SECRET,
            },
        }
        return SimpleNamespace(model_dump=lambda: wire)

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=acompletion))
    assert (
        main(
            ["demo", "--company", "cloudscale-ai", "--provider", "live", "--output", str(tmp_path)]
        )
        == 0
    )
    trace_path = tmp_path / "cloudscale-ai/trace.jsonl"
    trace = [json.loads(line) for line in trace_path.read_text().splitlines()]
    turns = [event for event in trace if event["event"] == "provider_turn"]
    assert [turn["finish_reason"] for turn in turns] == ["tool_calls"] * 3 + [finish_reason]
    assert all(
        turn["usage"]
        == {
            "prompt_tokens": 1200,
            "completion_tokens": 4096,
            "total_tokens": 5296,
            "reasoning_tokens": 3800,
        }
        for turn in turns
    )
    assert SECRET not in trace_path.read_text()
    report = json.loads((tmp_path / "cloudscale-ai/report.json").read_text())
    assert "usage" not in report and report["status"] == "complete"


@pytest.mark.parametrize("usage", [None, {"prompt_tokens": 0}])
def test_missing_metadata_is_unknown_and_zero_is_preserved(live_env, monkeypatch, usage):
    async def acompletion(**kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": "{}"}}], "usage": usage}

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=acompletion))
    turn = asyncio.run(LiveProvider().complete([], [], 5))
    assert turn.finish_reason is None
    if usage is None:
        assert turn.usage is None
    else:
        assert turn.usage.prompt_tokens == 0
        assert turn.usage.completion_tokens is None
        assert turn.usage.total_tokens is None
        assert turn.usage.reasoning_tokens is None


def test_invalid_metadata_does_not_leak_or_break_provider(live_env, monkeypatch):
    async def acompletion(**kwargs):
        return {
            "choices": [
                {"message": {"role": "assistant", "content": "{}"}, "finish_reason": SECRET}
            ],
            "usage": {
                "prompt_tokens": SECRET,
                "completion_tokens": True,
                "total_tokens": -1,
                "completion_tokens_details": {"reasoning_tokens": 2.5},
            },
        }

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=acompletion))
    turn = asyncio.run(LiveProvider().complete([], [], 5))
    assert turn.finish_reason == "unknown"
    assert turn.usage.prompt_tokens is None
    assert turn.usage.completion_tokens is None
    assert turn.usage.total_tokens is None
    assert turn.usage.reasoning_tokens is None


def test_mock_trace_has_no_invented_token_usage(tmp_path):
    assert main(["demo", "--company", "cloudscale-ai", "--output", str(tmp_path)]) == 0
    trace = [
        json.loads(line)
        for line in (tmp_path / "cloudscale-ai/trace.jsonl").read_text().splitlines()
    ]
    turns = [event for event in trace if event["event"] == "provider_turn"]
    assert turns and all(turn["finish_reason"] is None and turn["usage"] is None for turn in turns)

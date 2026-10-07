"""Ordinary dialogue is reviewed without financial DTO requirements.

Scripted drafts/verdicts test the protocol, not live model reliability.
"""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_chat import Scripted, events

from fintech_copilot.chat_console import render_reply
from fintech_copilot.domain.generator import generate
from fintech_copilot.orchestration.agent import Budgets
from fintech_copilot.orchestration.chat import (
    ChatReply,
    ChatSession,
    MockChatProvider,
    validate_reply,
)
from fintech_copilot.orchestration.chat_reply import canonical_composed, fact_aliases
from fintech_copilot.orchestration.conversation import ConversationReply, conversation_candidate
from fintech_copilot.orchestration.narrative_review import REVIEW_PROMPT, NarrativeReview
from fintech_copilot.orchestration.providers import Call, LiveProvider, Turn
from fintech_copilot.tools.dispatcher import TOOLS, Dispatcher


def test_greeting_then_expenses_uses_actual_tools_for_each_company(scenarios):
    for cid in ("apex-logistics", "cloudscale-ai", "harbour-trade"):
        dataset = generate(scenarios, cid, 42)
        session = ChatSession(dataset, MockChatProvider())
        greeting = asyncio.run(session.send("Привет"))
        assert greeting.error_code is None and greeting.reply.kind == "help"
        expenses = asyncio.run(session.send("Разложи расходы по категориям"))
        assert expenses.error_code is None and isinstance(expenses.reply, ChatReply)
        assert [e["tool"] for e in events(expenses, "tool_call")] == [TOOLS[1]]
        expected = Dispatcher(dataset).dispatch(TOOLS[1], dataset.scope.model_dump_json())
        assert expenses.tool_results == (expected,)
        assert validate_reply(expenses.reply, expenses.tool_results, dataset) == expenses.facts


def test_conversation_does_not_publish_financial_claims_from_cache(scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    session = ChatSession(dataset, MockChatProvider())
    first = asyncio.run(session.send("Какой runway?"))
    assert first.error_code is None and first.facts
    provider = Scripted(
        [
            Turn(content="Привет! У компании осталось 270000.00 HKD."),
            Turn(content="Привет! Я на связи. Чем помочь?"),
        ]
    )
    provider.reviews = [
        NarrativeReview(supported=False, issues=("unsupported_claim",)),
        NarrativeReview(supported=True),
    ]
    session.provider = provider
    run = asyncio.run(session.send("Привет"))
    assert run.error_code is None and isinstance(run.reply, ConversationReply)
    assert "270000" not in render_reply(run, dataset)
    assert run.facts == () and len(run.tool_results) == 1
    assert [e["supported"] for e in events(run, "narrative_review")] == [False, True]
    repair_prompt = provider.requests[2][0][0]["content"]
    assert "ComposedReply JSON schema" not in repair_prompt
    assert '"message"' in repair_prompt


def test_skipped_financial_request_reopens_tools_instead_of_publishing_prose(scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    mock_session = ChatSession(dataset, MockChatProvider())
    expected = asyncio.run(mock_session.send("Расходы"))
    assert expected.error_code is None
    result = expected.tool_results[0]
    aliases = fact_aliases((result,))
    from fintech_copilot.orchestration.chat_reply import ComposedReply, FactPart, TextPart

    drafted = ComposedReply(
        kind="answer",
        parts=(TextPart(text="Операционные расходы — "), FactPart(ref=next(iter(aliases)))),
    )
    canonical = ChatReply.model_validate(canonical_composed(drafted, aliases, dataset))
    provider = Scripted(
        [
            Turn(content="Конечно! Сейчас разложу расходы."),
            Turn(calls=(Call("expenses", TOOLS[1], dataset.scope.model_dump_json()),)),
            Turn(content=canonical.model_dump_json()),
        ]
    )
    provider.reviews = [
        NarrativeReview(supported=False, issues=("missing_requested_part",)),
        NarrativeReview(supported=True),
    ]
    run = asyncio.run(ChatSession(dataset, provider).send("Разложи расходы"))
    assert run.error_code is None and isinstance(run.reply, ChatReply)
    assert run.plan and run.tool_results == (result,)
    assert events(run, "grounding_rejected")[0]["review_issues"] == ["missing_requested_part"]
    assert provider.requests[2][1]  # repair retains native tools for planning
    assert validate_reply(run.reply, run.tool_results, dataset) == run.facts


def test_ordinary_text_cannot_finish_an_active_financial_plan(scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    provider = Scripted(
        [
            Turn(calls=(Call("expenses", TOOLS[1], dataset.scope.model_dump_json()),)),
            Turn(content="Привет! Чем помочь?"),
            Turn(content="Привет! Чем помочь?"),
        ]
    )
    run = asyncio.run(ChatSession(dataset, provider).send("Расходы"))
    assert run.error_code == "INVALID_REPLY" and run.reply is None
    assert run.plan and run.tool_results


@pytest.mark.parametrize("budget", [1, 2])
def test_greeting_review_failure_or_exhausted_budget_cannot_publish(scenarios, budget):
    class InvalidReview(Scripted):
        async def complete(self, messages, tools, timeout):
            if messages[0]["content"].startswith(REVIEW_PROMPT):
                return Turn(content='{"supported":"yes"}')
            return await super().complete(messages, tools, timeout)

    dataset = generate(scenarios, "cloudscale-ai", 42)
    run = asyncio.run(
        ChatSession(
            dataset, InvalidReview([Turn(content="Привет!")]), Budgets(provider_turns=budget)
        ).send("Привет")
    )
    assert run.error_code == ("TURN_LIMIT" if budget == 1 else "INVALID_REVIEW")
    assert run.reply is None and run.facts == ()


@pytest.mark.parametrize("unsafe", ["Привет {{fact:f0}}", "Привет\x1b[31m", "Привет\u202e"])
def test_conversation_never_passes_renderer_placeholders_or_terminal_controls(unsafe):
    with pytest.raises(ValidationError):
        ConversationReply(text=unsafe)


def test_conversation_has_no_financial_fields_and_does_not_parse_fact_selections(scenarios):
    with pytest.raises(ValidationError):
        ConversationReply(text="Расходы", references=({"tool_result_id": "r", "fact_id": "f"},))
    dataset = generate(scenarios, "cloudscale-ai", 42)
    assert (
        conversation_candidate('{"kind":"answer","parts":[{"type":"fact","ref":"f0"}]}', dataset)
        is None
    )
    with pytest.raises(ValidationError):
        ChatReply(text="Привет!", kind="answer")  # financial invariant still intact


def test_live_adapter_accepts_plain_greeting_without_forcing_financial_json(scenarios, monkeypatch):
    """Offline SDK response fixture, never a paid/live-model check."""
    monkeypatch.setenv("LLM_MODEL", "litellm_proxy/synthetic-model")
    monkeypatch.setenv("LITELLM_PROXY_API_BASE", "https://gateway.test/v1")
    monkeypatch.setenv("LITELLM_PROXY_API_KEY", "synthetic-conversation-test-key")
    requests = []

    async def acompletion(**kwargs):
        requests.append(kwargs)
        if kwargs["messages"][0]["content"].startswith(REVIEW_PROMPT):
            text = NarrativeReview(supported=True).model_dump_json()
        else:
            text = "Привет! Чем помочь?"
        return {"choices": [{"message": {"role": "assistant", "content": text}}]}

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=acompletion))
    dataset = generate(scenarios, "cloudscale-ai", 42)
    run = asyncio.run(ChatSession(dataset, LiveProvider()).send("Привет"))
    assert run.error_code is None and render_reply(run, dataset) == "Привет! Чем помочь?"
    assert requests[0]["tools"] and "response_format" not in requests[0]
    assert requests[1]["response_format"] == {"type": "json_object"}
    assert not requests[1].get("tools")
    assert "synthetic-conversation-test-key" not in json.dumps(run.trace)

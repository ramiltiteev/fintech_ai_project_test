"""Typed value selection and compiled provenance use real financial tools."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError
from test_chat import Scripted

from fintech_copilot.chat_console import render_reply
from fintech_copilot.domain.generator import generate
from fintech_copilot.orchestration.chat import (
    ChatReply,
    ChatSession,
    MockChatProvider,
    validate_reply,
)
from fintech_copilot.orchestration.chat_reply import (
    ComposedReply,
    ContextPart,
    FactPart,
    TextPart,
    canonical_composed,
    fact_aliases,
)
from fintech_copilot.orchestration.grounding import GroundingError, validate_synthesis
from fintech_copilot.orchestration.narrative_review import REVIEW_PROMPT, NarrativeReview
from fintech_copilot.orchestration.providers import Call, Turn
from fintech_copilot.tools.dispatcher import TOOLS, Dispatcher


class NaturalReport(MockChatProvider):
    async def complete(self, messages, tools, timeout):
        turn = await super().complete(messages, tools, timeout)
        if turn.calls or messages[0]["content"].startswith(REVIEW_PROMPT):
            return turn
        reply = ComposedReply.model_validate_json(turn.content)
        if reply.kind == "report":
            reply = reply.model_copy(
                update={
                    "parts": (
                        TextPart(text="Отчёт для "),
                        ContextPart(field="company_name"),
                        TextPart(text=". Период ["),
                        ContextPart(field="period_start"),
                        TextPart(text=", "),
                        ContextPart(field="period_end"),
                        TextPart(text="). Рассмотрены четыре направления.\n\n"),
                        *reply.parts,
                    )
                }
            )
        return Turn(content=reply.model_dump_json())


@pytest.mark.parametrize("cid", ["apex-logistics", "cloudscale-ai", "harbour-trade"])
def test_natural_report_with_snapshot_dates_compiles_and_keeps_full_provenance(cid, scenarios):
    dataset = generate(scenarios, cid, 42)
    run = asyncio.run(ChatSession(dataset, NaturalReport()).send("Подготовь полный отчет"))
    assert run.error_code is None and run.report.status == "complete"
    assert not any(e["event"] == "grounding_rejected" for e in run.trace)
    rendered = render_reply(run, dataset)
    assert "Рассмотрены четыре направления" in rendered
    assert "[2026-07-01, 2026-10-01)" in rendered and dataset.company.name in rendered
    restored = ChatReply.model_validate_json(run.reply.model_dump_json())
    assert validate_reply(restored, run.tool_results, dataset) == run.facts
    validate_synthesis(run.report.synthesis, run.report.tool_results, dataset)
    dispatcher = Dispatcher(dataset)
    for result in run.tool_results:
        assert result == dispatcher.dispatch(result.tool_name, dataset.scope.model_dump_json())


@pytest.mark.parametrize(
    "part",
    [
        {"type": "text", "text": "Расходы 999999 HKD"},
        {"type": "text", "text": "Runway Ⅲ месяца"},
        {"type": "text", "text": "Сумма ٢٢٠"},
        {"type": "text", "text": "{{fact:f0}}"},
        {"type": "text", "text": "\u001b[2J"},
        {"type": "fact", "ref": "f0", "value": "999999.00"},
        {"type": "fact", "ref": "f0", "amount": 1.5},
        {"type": "context", "field": "closing_cash"},
        {"type": "context", "field": "period_start", "value": "1999-01-01"},
    ],
)
def test_model_cannot_insert_raw_values_or_override_sources(part):
    with pytest.raises(ValidationError):
        ComposedReply.model_validate({"kind": "answer", "parts": [part]})


@pytest.mark.parametrize("mutation", ["text", "reference", "unknown", "context"])
def test_tampered_compiled_reply_cannot_bypass_source_validation(mutation, scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    result = Dispatcher(dataset).dispatch(TOOLS[0], dataset.scope.model_dump_json())
    aliases = fact_aliases((result,))
    draft = ComposedReply(
        kind="answer",
        parts=(TextPart(text="Остаток "), FactPart(ref="f1"), ContextPart(field="period_start")),
    )
    raw = canonical_composed(draft, aliases, dataset)
    if mutation == "text":
        raw["text"] += " Выручка 999999 HKD."
    elif mutation == "context":
        raw["text"] = raw["text"].replace("2026-07-01", "1999-01-01")
    else:
        raw["parts"][1]["reference"]["fact_id"] = (
            "result:unknown:closing_cash" if mutation == "unknown" else result.facts[0].fact_id
        )
    candidate = ChatReply.model_validate(raw)
    with pytest.raises(GroundingError):
        validate_reply(candidate, (result,), dataset)


def test_persisted_fact_parts_do_not_depend_on_session_alias_indices(scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    session = ChatSession(dataset, MockChatProvider())
    assert asyncio.run(session.send("Какой runway?")).error_code is None
    run = asyncio.run(session.send("Разложи расходы"))
    assert run.error_code is None and run.reply.parts is not None
    only_opex = tuple(r for r in run.tool_results if r.tool_name == TOOLS[1])
    assert validate_reply(run.reply, only_opex, dataset) == run.facts
    serialized = run.reply.model_dump(mode="json")
    assert all(
        "reference" in p and "ref" not in p for p in serialized["parts"] if p["type"] == "fact"
    )


def test_raw_text_part_repair_receives_exact_candidate_and_preserves_full_report(scenarios):
    class BadValue(NaturalReport):
        invalid_sent = False
        feedback = None

        async def complete(self, messages, tools, timeout):
            for message in messages:
                if (message.get("content") or "").startswith("Reply rejected."):
                    self.feedback = message["content"]
            turn = await super().complete(messages, tools, timeout)
            if not turn.calls and not self.invalid_sent:
                raw = json.loads(turn.content)
                if raw.get("kind") == "report":
                    self.invalid_sent = True
                    raw["parts"][0]["text"] = "synthetic-private Сумма 999999 HKD "
                    return Turn(content=json.dumps(raw))
            return turn

    dataset = generate(scenarios, "cloudscale-ai", 42)
    provider = BadValue()
    run = asyncio.run(ChatSession(dataset, provider).send("Подготовь полный отчет"))
    assert run.error_code is None and run.report.status == "complete"
    rejected = next(e for e in run.trace if e["event"] == "grounding_rejected")
    assert rejected["validation_errors"][0]["code"] == "raw_quantity_in_text"
    assert rejected["validation_errors"][0]["path"] == ["parts", 0, "text", "text"]
    assert "synthetic-private" in provider.feedback
    assert "synthetic-private" not in json.dumps(run.trace)
    assert "999999" not in render_reply(run, dataset)


def test_financial_quantity_in_words_is_gated_by_semantic_review(scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    bad = ComposedReply(
        kind="answer",
        parts=(TextPart(text="Расходы пять миллионов. Факт: "), FactPart(ref="f0")),
    )
    good = ComposedReply(kind="answer", parts=(TextPart(text="Расходы — "), FactPart(ref="f0")))
    provider = Scripted(
        [
            Turn(calls=(Call("expenses", TOOLS[1], dataset.scope.model_dump_json()),)),
            Turn(content=bad.model_dump_json()),
            Turn(content=good.model_dump_json()),
        ]
    )
    provider.reviews = [
        NarrativeReview(supported=False, issues=("unsupported_claim",)),
        NarrativeReview(supported=True),
    ]
    run = asyncio.run(ChatSession(dataset, provider).send("Покажи расходы"))
    assert run.error_code is None and "пять миллионов" not in run.reply.text
    assert [e["supported"] for e in run.trace if e["event"] == "narrative_review"] == [False, True]


def test_invalid_reviewer_blocks_typed_response(scenarios):
    class BrokenReview(NaturalReport):
        async def complete(self, messages, tools, timeout):
            if messages[0]["content"].startswith(REVIEW_PROMPT):
                return Turn(content='{"supported":"yes"}')
            return await super().complete(messages, tools, timeout)

    dataset = generate(scenarios, "cloudscale-ai", 42)
    run = asyncio.run(ChatSession(dataset, BrokenReview()).send("Подготовь полный отчет"))
    assert run.error_code == "INVALID_REVIEW" and run.reply is None and run.report is None

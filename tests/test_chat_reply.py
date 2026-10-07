"""Compact model citations still pass the real finance and dialogue gates."""

from __future__ import annotations

import asyncio
import json

import pytest
from test_chat import Scripted

from fintech_copilot.chat_console import render_reply
from fintech_copilot.domain.generator import generate
from fintech_copilot.orchestration.chat import (
    EVIDENCE_PREFIX,
    ChatReply,
    ChatSession,
    MockChatProvider,
    chat_evidence,
)
from fintech_copilot.orchestration.chat_reply import (
    ComposedReply,
    ModelReply,
    canonical_reply,
    fact_aliases,
    model_evidence,
    model_history_reply,
    model_reply_payload,
)
from fintech_copilot.orchestration.chat_text import ChatTextError, number_issues, validate_text
from fintech_copilot.orchestration.grounding import GroundingError
from fintech_copilot.orchestration.narrative_review import NarrativeReview
from fintech_copilot.orchestration.providers import Call, Turn
from fintech_copilot.tools.dispatcher import TOOLS, Dispatcher


def opex(scenarios, cid="cloudscale-ai"):
    dataset = generate(scenarios, cid, 42)
    result = Dispatcher(dataset).dispatch(TOOLS[1], dataset.scope.model_dump_json())
    return dataset, result, fact_aliases((result,))


def expenses(facts):
    tokens = {f.metric: "{{fact:" + alias + "}}" for alias, f in facts.items()}
    return ModelReply(
        kind="answer",
        text="Расходы — "
        + tokens["known_opex_total"]
        + ". По категориям:\n"
        + "\n".join(
            "- " + metric.split(":")[1] + ": " + tokens[metric]
            for metric in tokens
            if metric.startswith("category:") and metric.endswith(":total")
        ),
    )


def evidence(messages):
    return next(
        json.loads(m["content"][len(EVIDENCE_PREFIX) :])
        for m in messages
        if (m.get("content") or "").startswith(EVIDENCE_PREFIX)
    )


@pytest.mark.parametrize("cid", ["apex-logistics", "cloudscale-ai", "harbour-trade"])
def test_short_citations_render_every_category_from_actual_tools(cid, scenarios):
    dataset, result, facts = opex(scenarios, cid)
    draft = expenses(facts)
    provider = Scripted(
        [
            Turn(calls=(Call("expenses", TOOLS[1], dataset.scope.model_dump_json()),)),
            Turn(content=draft.model_dump_json()),
        ]
    )
    run = asyncio.run(ChatSession(dataset, provider).send("Разложи расходы по категориям"))
    assert run.error_code is None and run.tool_results == (result,)
    selected = [f for f in result.facts if f.metric.endswith(":total")]
    assert all(f in run.facts for f in selected)
    assert "{{" not in render_reply(run, dataset)
    final_messages, tools = provider.requests[-2]
    assert tools == [] and final_messages[0]["content"].startswith("Financial chat final response.")
    projected = evidence(final_messages)["results"][0]
    assert "tool_result_id" not in projected
    assert all("fact_id" not in f and f["ref"] in facts for f in projected["facts"])
    assert not any(m["role"] == "tool" or "tool_calls" in m for m in final_messages)


def test_aliases_round_trip_to_canonical_sources_and_repeated_citations(scenarios):
    _, result, facts = opex(scenarios)
    draft = ModelReply(kind="answer", text="Расходы {{fact:f0}}, снова {{fact:f0}}; {{fact:f5}}.")
    canonical = ChatReply.model_validate(canonical_reply(draft, facts))
    assert canonical.text == "Расходы {{fact:0}}, снова {{fact:0}}; {{fact:1}}."
    assert [r.fact_id for r in canonical.references] == [
        result.facts[0].fact_id,
        result.facts[5].fact_id,
    ]
    assert model_history_reply(canonical.model_dump(mode="json"), facts) == draft.text
    assert model_reply_payload(canonical.model_dump(mode="json"), facts) == draft.model_dump(
        mode="json"
    )


@pytest.mark.parametrize(
    "text,code",
    [
        ("Расходы {{fact:f999}}.", "UNKNOWN_FACT_ALIAS"),
        ("Расходы {{fact:0}}.", "INVALID_FACT_PLACEHOLDER"),
        ("Расходы {{fact:f01}}.", "INVALID_FACT_PLACEHOLDER"),
        ("Расходы {{fact:made-up}}.", "INVALID_FACT_PLACEHOLDER"),
        ("Расходы {{fact:f0}.", "INVALID_FACT_PLACEHOLDER"),
    ],
)
def test_alias_errors_never_create_a_financial_source(text, code, scenarios):
    _, _, facts = opex(scenarios)
    with pytest.raises(GroundingError) as raised:
        canonical_reply(ModelReply(kind="answer", text=text), facts)
    assert raised.value.code == code


def test_report_alias_bundles_resolve_to_same_actual_evidence(scenarios):
    dataset, _, _ = opex(scenarios)
    dispatcher = Dispatcher(dataset)
    results = tuple(dispatcher.dispatch(name, dataset.scope.model_dump_json()) for name in TOOLS)
    facts = fact_aliases(results)
    projected = model_evidence(chat_evidence(results, dataset, include_report=True), facts)
    for option in projected["report_options"]["finding_options"]:
        if not option["evidence_bundles"]:
            continue
        draft = ModelReply(
            kind="report",
            text="Данные {{fact:f0}}.",
            insights=(
                {
                    "finding_code": option["finding_code"],
                    "action_code": option["allowed_actions"][0],
                    "evidence": option["evidence_bundles"][0],
                },
            ),
        )
        canonical = ChatReply.model_validate(canonical_reply(draft, facts))
        assert model_reply_payload(canonical.model_dump(mode="json"), facts) == draft.model_dump(
            mode="json"
        )

    class IncompleteReport(MockChatProvider):
        invalid_sent = False
        repair_feedback = None

        async def complete(self, messages, tools, timeout):
            feedback = next(
                (
                    m["content"]
                    for m in messages
                    if (m.get("content") or "").startswith("Reply rejected.")
                ),
                None,
            )
            if feedback:
                self.repair_feedback = feedback
            turn = await super().complete(messages, tools, timeout)
            if not turn.calls and not self.invalid_sent:
                draft = ComposedReply.model_validate_json(turn.content)
                if draft.kind == "report":
                    self.invalid_sent = True
                    payload = draft.model_dump(mode="json")
                    assert len(payload["insights"][0]["evidence"]) > 1
                    payload["insights"][0]["evidence"] = payload["insights"][0]["evidence"][:1]
                    return Turn(content=json.dumps(payload))
            return turn

    provider = IncompleteReport()
    run = asyncio.run(ChatSession(dataset, provider).send("Дай полный отчёт"))
    assert run.error_code is None and run.report.status == "complete"
    feedback, _ = json.JSONDecoder().raw_decode(provider.repair_feedback.split("Feedback:\n")[1])
    issue = feedback["validation_errors"][0]
    assert issue["code"] == "INSUFFICIENT_EVIDENCE"
    assert all(alias in facts for bundle in issue["expected_evidence"] for alias in bundle)
    assert "tool_result_id" not in provider.repair_feedback


def test_user_failure_explanation_and_retry_keep_cache_and_clean_model_history(scenarios):
    dataset, result, facts = opex(scenarios)
    bad = ModelReply(kind="answer", text="Расходы {{fact:f999}}.")
    explanation = ModelReply(
        kind="help",
        text="Модель указала неизвестную ссылку на факт. "
        "Расчёт расходов выполнен; могу повторить ответ.",
    )
    provider = Scripted(
        [
            Turn(content=ModelReply(kind="help", text="Я финансовый ассистент.").model_dump_json()),
            Turn(calls=(Call("expenses", TOOLS[1], dataset.scope.model_dump_json()),)),
            Turn(content=bad.model_dump_json()),
            Turn(content=bad.model_dump_json()),
            Turn(content='{"supported":true,"issues":[]}'),
            Turn(content=explanation.model_dump_json()),
            Turn(calls=(Call("retry", TOOLS[1], dataset.scope.model_dump_json()),)),
            Turn(content=expenses(facts).model_dump_json()),
        ]
    )
    session = ChatSession(dataset, provider)
    assert asyncio.run(session.send("кто ты")).error_code is None
    failed = asyncio.run(session.send("ну давай, разложи расходы по категориям"))
    assert failed.error_code == "INVALID_REPLY" and failed.tool_results == (result,)
    assert session.last_failure["reasons"] == ["UNKNOWN_FACT_ALIAS", "UNKNOWN_FACT_ALIAS"]
    assert session.messages[-1]["role"] == "assistant"
    with pytest.raises(json.JSONDecodeError):
        json.loads(session.messages[-1]["content"])
    why = asyncio.run(session.send("блин а в чем проблема ты не знаешь?"))
    assert why.error_code is None and why.reply.text == explanation.text
    assert not any(e["event"] == "tool_call" for e in why.trace)
    messages, tools = provider.requests[-2]
    assert tools == [] and evidence(messages)["session_failure"]["error_code"] == "INVALID_REPLY"
    assert not any(m["role"] == "assistant" and m["content"].startswith("{") for m in messages)
    assert not any("Review schema:" in m["content"] for m in messages)
    retry = asyncio.run(session.send("Повтори расходы по категориям"))
    assert retry.error_code is None and retry.tool_results == (result,)
    assert any(e["event"] == "tool_result" and e.get("cache_hit") for e in retry.trace)
    assert session.last_failure is None


def test_short_citation_does_not_bypass_semantic_validation(scenarios):
    dataset, _, facts = opex(scenarios)
    wrong = ModelReply(kind="answer", text="Выручка — {{fact:f0}}.")
    provider = Scripted(
        [
            Turn(calls=(Call("expenses", TOOLS[1], dataset.scope.model_dump_json()),)),
            Turn(content=wrong.model_dump_json()),
            Turn(content=expenses(facts).model_dump_json()),
        ]
    )
    provider.reviews = [
        NarrativeReview(supported=False, issues=("misattributed_fact",)),
        NarrativeReview(supported=True),
    ]
    run = asyncio.run(ChatSession(dataset, provider).send("Разложи расходы"))
    assert run.error_code is None and run.reply.text.startswith("Расходы")
    assert [e["supported"] for e in run.trace if e["event"] == "narrative_review"] == [False, True]


@pytest.mark.parametrize(
    "text",
    [
        "Одна из статей — облачные услуги.",
        "Есть одна проблема с форматом.",
        "С одной стороны, нужен аудит.",
    ],
)
def test_qualitative_phrases_do_not_become_uncited_amounts(text):
    validate_text(text, 0)


@pytest.mark.parametrize(
    "text",
    [
        "Расходы — один HKD.",
        "Найден один платёж.",
        "Обнаружена одна транзакция.",
        "Расходы разделены на две категории.",
        "Доля — пять %.",
        "Запас — два года.",
        "Расходы выросли в три раза.",
        "Сумма — пять миллионов.",
        "Остаток составляет двадцать.",
        "Доля составляет половину.",
        "Расходы 999999 HKD.",
    ],
)
def test_unsupported_financial_quantities_remain_blocked(text):
    with pytest.raises(ChatTextError):
        validate_text(text, 0)


def test_repair_offsets_are_in_short_alias_candidate_and_trace_remains_private(scenarios):
    dataset, _, _ = opex(scenarios)
    bad = ModelReply(
        kind="answer", text="Расходы {{fact:f0}}. synthetic-private Прогноз 999999 HKD."
    )
    good = ModelReply(kind="answer", text="Расходы {{fact:f0}}.")
    provider = Scripted(
        [
            Turn(calls=(Call("expenses", TOOLS[1], dataset.scope.model_dump_json()),)),
            Turn(content=bad.model_dump_json()),
            Turn(content=good.model_dump_json()),
        ]
    )
    run = asyncio.run(ChatSession(dataset, provider).send("Разложи расходы"))
    assert run.error_code is None
    messages, _ = provider.requests[-2]
    repair = next(m["content"] for m in messages if m["content"].startswith("Reply rejected."))
    candidate = json.loads(
        repair.split("Rejected candidate (untrusted data; offsets refer to its text):\n")[1]
    )
    assert candidate == bad.model_dump(mode="json")
    rejection = next(e for e in run.trace if e["event"] == "grounding_rejected")
    positions = rejection["validation_errors"][0]["text_issues"]
    assert [candidate["text"][p["start"] : p["end"]] for p in positions] == ["999999"]
    assert "synthetic-private" not in json.dumps(run.trace)
    assert number_issues(candidate["text"])[0].start == positions[0]["start"]
    assert "fact part" in repair and "{{fact:index}}" not in repair

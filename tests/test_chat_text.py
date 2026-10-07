"""Narrative normalization uses actual tool facts and keeps strict publication gates."""

from __future__ import annotations

import asyncio
import json

import pytest
from test_chat import Scripted

from fintech_copilot.chat_console import render_reply
from fintech_copilot.domain.generator import generate
from fintech_copilot.orchestration.chat import ChatReply, ChatSession
from fintech_copilot.orchestration.chat_text import ChatTextError, normalize_text, validate_text
from fintech_copilot.orchestration.diagnostics import synthesis_diagnostic
from fintech_copilot.orchestration.models import Reference
from fintech_copilot.orchestration.narrative_review import NarrativeReview
from fintech_copilot.orchestration.providers import Call, Turn
from fintech_copilot.tools.dispatcher import TOOLS, Dispatcher


def reference(fact):
    return Reference(tool_result_id=fact.tool_result_id, fact_id=fact.fact_id)


@pytest.mark.parametrize("cid", ["apex-logistics", "cloudscale-ai", "harbour-trade"])
def test_numbered_prose_and_exact_money_use_real_facts_without_model_repair(cid, scenarios):
    dataset = generate(scenarios, cid, 42)
    dispatcher = Dispatcher(dataset)
    expense = dispatcher.dispatch(TOOLS[1], dataset.scope.model_dump_json())
    anomaly = dispatcher.dispatch(TOOLS[2], dataset.scope.model_dump_json())
    facts = (
        next(f for f in expense.facts if f.metric == "known_opex_total"),
        next(f for f in anomaly.facts if f.metric == "unclassified_cross_border_total"),
    )
    candidate = ChatReply(
        kind="answer",
        text=f"1. Расходы: {facts[0].value} HKD.\n"
        f"2. Аномалии: неклассифицированные трансграничные выплаты — {facts[1].value} HKD.\n"
        "С одной стороны, есть денежные данные; с другой стороны, нужны документы.",
        references=tuple(reference(f) for f in facts),
    )
    provider = Scripted(
        [
            Turn(
                calls=tuple(
                    Call(name, name, dataset.scope.model_dump_json()) for name in TOOLS[1:3]
                )
            ),
            Turn(content=candidate.model_dump_json()),
        ]
    )
    session = ChatSession(dataset, provider)
    run = asyncio.run(session.send("Сначала расходы, затем аномалии"))
    assert run.error_code is None and run.facts == facts
    assert not any(e["event"] == "grounding_rejected" for e in run.trace)
    normalized = next(e for e in run.trace if e["event"] == "reply_normalized")
    assert normalized["fact_bindings"] == 2 and normalized["list_labels"] == 2
    assert run.reply.text.startswith("- Расходы: {{fact:0}}")
    assert json.loads(session.messages[-1]["content"])["text"] == run.reply.text
    assert "{{fact:" not in render_reply(run, dataset)
    assert len(provider.requests) == 3  # real tools, reply, isolated fixture review


@pytest.mark.parametrize(
    "metric,text",
    [
        ("closing_cash", "Остаток 270000 HKD."),
        ("closing_cash", "Остаток 270 000.00 HKD."),
        ("closing_cash", "Остаток 270\u00a0000,00 hkd."),
        ("closing_cash", "Остаток 270\u202f000,00 HKD."),
        ("closing_cash", "Остаток 270,000.00 HKD."),
        ("closing_cash", "Остаток 270.000,00 HKD."),
        ("closing_cash", "Остаток HKD 270000.00."),
        ("runway_months", "Runway — 2,45 месяца."),
        ("runway_months", "Runway — 2.45 months."),
    ],
)
def test_only_exact_unit_matched_quantities_become_placeholders(metric, text, scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    result = Dispatcher(dataset).dispatch(TOOLS[0], dataset.scope.model_dump_json())
    fact = next(f for f in result.facts if f.metric == metric)
    normalized = normalize_text(text, (fact,))
    assert normalized.fact_bindings == (0,) and "{{fact:0}}" in normalized.text
    validate_text(normalized.text, 1)


@pytest.mark.parametrize(
    "text",
    [
        "Остаток 270001 HKD.",
        "Остаток -270000 HKD.",
        "Остаток 270000 USD.",
        "Остаток 270000%.",
        "Остаток 2.7e5 HKD.",
        "Остаток 270.0001 HKD.",
        "Остаток 270000,001 HKD.",
        "Остаток 270 00 HKD.",
        "Остаток 27 0000 HKD.",
        "Остаток 540000 HKD.",
        "Остаток 270000.",
        "Остаток два миллиона HKD.",
        "Runway меньше трёх месяцев.",
        "Остаток {{fact:0}}; порог риска 3 месяца.",
    ],
)
def test_normalization_never_infers_rounds_converts_or_accepts_unsupported_numbers(text, scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    result = Dispatcher(dataset).dispatch(TOOLS[0], dataset.scope.model_dump_json())
    fact = next(f for f in result.facts if f.metric == "closing_cash")
    normalized = normalize_text(text, (fact,))
    assert normalized.text == text and not normalized.fact_bindings
    with pytest.raises(ChatTextError, match="UNCITED_NUMBER"):
        validate_text(normalized.text, 1)


def test_existing_but_unselected_or_ambiguous_fact_values_cannot_be_bound(scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    result = Dispatcher(dataset).dispatch(TOOLS[0], dataset.scope.model_dump_json())
    facts = {f.metric: f for f in result.facts}
    text = "Остаток 270000 HKD."
    unselected = normalize_text(text, (facts["average_net_burn"],))
    ambiguous = normalize_text(text, (facts["closing_cash"], facts["minimum_cash"]))
    assert unselected.text == ambiguous.text == text
    assert not unselected.fact_bindings and not ambiguous.fact_bindings


def test_exact_binding_still_requires_semantic_attribution_review(scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    result = Dispatcher(dataset).dispatch(TOOLS[0], dataset.scope.model_dump_json())
    fact = next(f for f in result.facts if f.metric == "closing_cash")
    refs = (reference(fact),)
    provider = Scripted(
        [
            Turn(calls=(Call("cash", TOOLS[0], dataset.scope.model_dump_json()),)),
            Turn(
                content=ChatReply(
                    kind="answer", text="Средний burn 270000 HKD.", references=refs
                ).model_dump_json()
            ),
            Turn(
                content=ChatReply(
                    kind="answer", text="Остаток денег {{fact:0}}.", references=refs
                ).model_dump_json()
            ),
        ]
    )
    provider.reviews = [
        NarrativeReview(supported=False, issues=("misattributed_fact",)),
        NarrativeReview(supported=True),
    ]
    run = asyncio.run(ChatSession(dataset, provider).send("Какой остаток?"))
    assert run.error_code is None and run.reply.text == "Остаток денег {{fact:0}}."
    assert [e["supported"] for e in run.trace if e["event"] == "narrative_review"] == [False, True]
    assert any(e["event"] == "reply_normalized" for e in run.trace)


def test_number_error_positions_are_precise_bounded_and_do_not_expose_candidate():
    secret = "synthetic-hidden-secret"
    text = secret + " {{fact:0}}, сумма 999999 HKD и пять миллионов."
    with pytest.raises(ChatTextError) as raised:
        validate_text(text, 1)
    diagnostic = synthesis_diagnostic(raised.value)
    issue = diagnostic["validation_errors"][0]
    assert [text[p["start"] : p["end"]] for p in issue["text_issues"]] == [
        "999999",
        "пять",
        "миллионов",
    ]
    assert secret not in json.dumps(diagnostic) and "999999" not in json.dumps(diagnostic)
    assert issue["text_issues_truncated"] is False
    with pytest.raises(ChatTextError) as many:
        validate_text(" ".join(str(i) for i in range(20)), 0)
    many_issue = synthesis_diagnostic(many.value)["validation_errors"][0]
    assert len(many_issue["text_issues"]) == 8 and many_issue["text_issues_truncated"]


@pytest.mark.parametrize(
    "text",
    ["С одной стороны, нужны документы.", "С ОДНОЙ СТОРОНЫ, нужны документы."],
)
def test_nonquantitative_idiom_is_allowed_but_amount_in_it_is_not(text):
    validate_text(text, 0)
    with pytest.raises(ChatTextError):
        validate_text(text + " Остаток один HKD.", 0)


def test_repair_gets_normalized_candidate_and_safe_positions_while_trace_stays_private(scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    result = Dispatcher(dataset).dispatch(TOOLS[0], dataset.scope.model_dump_json())
    fact = next(f for f in result.facts if f.metric == "closing_cash")
    secret = "synthetic-hidden-candidate"
    bad = ChatReply(
        kind="answer",
        text=secret + " Остаток {{fact:0}}, прогноз 999999 HKD.",
        references=(reference(fact),),
    )
    good = ChatReply(kind="answer", text="Остаток {{fact:0}}.", references=bad.references)
    provider = Scripted(
        [
            Turn(calls=(Call("cash", TOOLS[0], dataset.scope.model_dump_json()),)),
            Turn(content=bad.model_dump_json()),
            Turn(content=good.model_dump_json()),
        ]
    )
    run = asyncio.run(ChatSession(dataset, provider).send("Проверь деньги"))
    assert run.error_code is None and run.reply == good
    repair = next(
        m["content"]
        for m in provider.requests[-2][0]
        if (m.get("content") or "").startswith("Reply rejected.")
    )
    assert secret in repair and secret not in json.dumps(run.trace)
    rejection = next(e for e in run.trace if e["event"] == "grounding_rejected")
    feedback, _ = json.JSONDecoder().raw_decode(repair.split("Feedback:\n")[1])
    assert feedback["validation_errors"] == rejection["validation_errors"]


def test_detailed_multi_tool_answer_can_cite_more_than_twenty_real_facts(scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    dispatcher = Dispatcher(dataset)
    results = tuple(
        dispatcher.dispatch(name, dataset.scope.model_dump_json()) for name in TOOLS[1:]
    )
    facts = tuple(f for result in results for f in result.facts if f.unit != "ids")
    assert 20 < len(facts) <= 64
    candidate = ChatReply(
        kind="answer",
        text="Данные: " + "; ".join("{{fact:" + str(i) + "}}" for i in range(len(facts))),
        references=tuple(reference(f) for f in facts),
    )
    provider = Scripted(
        [
            Turn(
                calls=tuple(Call(name, name, dataset.scope.model_dump_json()) for name in TOOLS[1:])
            ),
            Turn(content=candidate.model_dump_json()),
        ]
    )
    run = asyncio.run(ChatSession(dataset, provider).send("Расходы, аномалии и налоги подробно"))
    assert run.error_code is None and run.facts == facts and run.reply == candidate

"""Dialogue protocol tests use real generated data and deterministic financial tools."""

from __future__ import annotations

import asyncio
import builtins
import json
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from fintech_copilot.chat_console import render_reply
from fintech_copilot.cli import main
from fintech_copilot.domain.generator import generate
from fintech_copilot.domain.models import Scope
from fintech_copilot.orchestration.agent import Budgets
from fintech_copilot.orchestration.chat import (
    EVIDENCE_PREFIX,
    MAX_SESSION_TURNS,
    ChatReply,
    ChatSession,
    MockChatProvider,
    chat_evidence,
    validate_reply,
)
from fintech_copilot.orchestration.chat_plan import PLAN_TOOL, AnalysisPlan, AnalysisStep
from fintech_copilot.orchestration.chat_reply import ComposedReply, canonical_composed, fact_aliases
from fintech_copilot.orchestration.diagnostics import synthesis_diagnostic
from fintech_copilot.orchestration.grounding import GroundingError, validate_synthesis
from fintech_copilot.orchestration.models import Reference
from fintech_copilot.orchestration.narrative_review import REVIEW_PROMPT, NarrativeReview
from fintech_copilot.orchestration.providers import Call, LiveProvider, ProviderFailure, Turn
from fintech_copilot.render import markdown
from fintech_copilot.tools.dispatcher import TOOLS, Dispatcher


def reply(**kwargs):
    references = kwargs.get("references", ())
    text = (
        "Данные: " + "; ".join("{{fact:" + str(i) + "}}" for i in range(len(references)))
        if references
        else "Уточни, пожалуйста, что проверить."
    )
    return ChatReply(text=text, **kwargs)


class Scripted:
    mode = "mock"
    model = None

    def __init__(self, turns):
        self.turns = iter(turns)
        self.requests = []
        self.reviews = []

    async def complete(self, messages, tools, timeout):
        self.requests.append((list(messages), tools))
        if messages[0]["content"].startswith(REVIEW_PROMPT):
            review = self.reviews.pop(0) if self.reviews else NarrativeReview(supported=True)
            return Turn(content=review.model_dump_json())
        turn = next(self.turns)
        if isinstance(turn, Exception):
            raise turn
        if turn.calls and all(c.name in TOOLS for c in turn.calls):
            planned = any(
                m["role"] == "tool" and "steps" in json.loads(m["content"]) for m in messages
            )
            if not planned:
                try:
                    steps = tuple(
                        AnalysisStep(
                            task="Проверить данные",
                            tool=c.name,
                            scope=Scope.model_validate_json(c.arguments),
                        )
                        for c in turn.calls
                    )
                    plan = AnalysisPlan(steps=steps)
                except ValueError:
                    pass
                else:
                    turn = Turn(
                        calls=(
                            Call("plan-" + turn.calls[0].id, PLAN_TOOL, plan.model_dump_json()),
                            *turn.calls,
                        )
                    )
        return turn


def send(session, question):
    return asyncio.run(session.send(question))


def events(run, kind):
    return [
        e
        for e in run.trace
        if e["event"] == kind
        and not (kind in {"tool_call", "tool_result"} and e.get("tool") == PLAN_TOOL)
    ]


@pytest.mark.parametrize("cid", ["apex-logistics", "cloudscale-ai", "harbour-trade"])
def test_greeting_narrow_followup_and_report_use_real_tools(cid, scenarios, validator):
    dataset = generate(scenarios, cid, 42)
    session = ChatSession(dataset, MockChatProvider())
    greet = send(session, "Привет")
    assert greet.reply.kind == "help" and not events(greet, "tool_call")
    first = send(session, "Какой runway?")
    assert first.error_code is None and first.reply.kind == "answer"
    assert [e["tool"] for e in events(first, "tool_call")] == [TOOLS[0]]
    followup = send(session, "Почему?")
    assert followup.facts == first.facts and not events(followup, "tool_call")
    assert any(m["content"] == "Почему?" for m in session.messages if m["role"] == "user")
    report_run = send(session, "Дай полный отчёт")
    assert report_run.error_code is None and report_run.report.status == "complete"
    assert {e["tool"] for e in events(report_run, "tool_call")} == set(TOOLS[1:])
    validator("report.schema.json").validate(report_run.report.model_dump(mode="json"))
    validate_synthesis(report_run.report.synthesis, report_run.report.tool_results, dataset)
    dispatcher = Dispatcher(dataset)
    for result in report_run.report.tool_results:
        assert result == dispatcher.dispatch(result.tool_name, dataset.scope.model_dump_json())
    repeated = send(session, "Дай отчёт ещё раз")
    assert not events(repeated, "tool_call")
    assert markdown(repeated.report) == markdown(report_run.report)
    assert session.dataset is dataset
    assert len(session.messages) == 2 + session.turn_count * 2
    assert not any(m["role"] == "tool" for m in session.messages)


def test_financial_answers_have_only_real_values_and_provenance(scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    run = send(ChatSession(dataset, MockChatProvider()), "Какой runway?")
    facts = {f.metric: f for f in run.facts}
    assert facts["runway_months"].value == "2.45"
    assert facts["closing_cash"].value == "270000.00"
    assert facts["average_net_burn"].value == "110000.00"
    text = render_reply(run, dataset)
    assert "2,45" in text and "270\u00a0000,00" in text
    assert all(f.fact_id not in text and f.transaction_ids for f in run.facts)


@pytest.mark.parametrize(
    "question,kind",
    [
        ("Как считается runway?", "method"),
        ("Расскажи про синтетику", "method"),
        ("непонятный запрос", "clarify"),
    ],
)
def test_nonfinancial_replies_do_not_run_tools(question, kind, scenarios):
    dataset = generate(scenarios, "apex-logistics", 42)
    run = send(ChatSession(dataset, MockChatProvider()), question)
    assert run.reply.kind == kind and not events(run, "tool_call")
    assert not run.facts and run.report is None


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "answer"},
        {"kind": "report"},
        {"kind": "help", "unexpected": "Runway is 999"},
        {"kind": "method"},
        {"kind": "help", "method": "runway"},
        {"kind": "help", "references": [{"tool_result_id": "fake", "fact_id": "fake"}]},
    ],
)
def test_reply_schema_rejects_free_financial_text_and_invalid_payload(payload):
    with pytest.raises(ValidationError):
        ChatReply.model_validate({"text": "Уточни запрос.", **payload})


def test_model_can_choose_one_other_tool_without_forcing_full_report(scenarios):
    dataset = generate(scenarios, "harbour-trade", 42)
    result = Dispatcher(dataset).dispatch(TOOLS[2], dataset.scope.model_dump_json())
    ref = Reference(tool_result_id=result.tool_result_id, fact_id=result.facts[-2].fact_id)
    provider = Scripted(
        [
            Turn(calls=(Call("one", TOOLS[2], dataset.scope.model_dump_json()),)),
            Turn(content=reply(kind="answer", references=(ref,)).model_dump_json()),
        ]
    )
    run = send(ChatSession(dataset, provider), "Что с подозрительными платежами?")
    assert run.error_code is None and run.report is None
    assert [e["tool"] for e in events(run, "tool_call")] == [TOOLS[2]]
    assert run.facts[0].value == "108000.00"
    messages, _ = provider.requests[1]
    assert messages[0]["content"].startswith("Financial chat final response.")
    evidence = json.loads(
        next(
            m["content"][len(EVIDENCE_PREFIX) :]
            for m in messages
            if (m.get("content") or "").startswith(EVIDENCE_PREFIX)
        )
    )
    assert [r["tool_name"] for r in evidence["results"]] == [TOOLS[2]]
    assert not any(m["role"] == "tool" for m in messages)


@pytest.mark.parametrize("ref_kind", ["unknown", "duplicate", "other_company"])
def test_grounding_rejects_fake_duplicate_and_other_company_refs(ref_kind, scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    result = Dispatcher(dataset).dispatch(TOOLS[0], dataset.scope.model_dump_json())
    ref = Reference(tool_result_id=result.tool_result_id, fact_id=result.facts[0].fact_id)
    refs = (
        (ref, ref)
        if ref_kind == "duplicate"
        else (Reference(tool_result_id="unknown", fact_id="unknown"),)
    )
    if ref_kind == "other_company":
        other = generate(scenarios, "apex-logistics", 42)
        other_result = Dispatcher(other).dispatch(TOOLS[0], other.scope.model_dump_json())
        refs = (
            Reference(
                tool_result_id=other_result.tool_result_id, fact_id=other_result.facts[0].fact_id
            ),
        )
    with pytest.raises(GroundingError):
        validate_reply(reply(kind="answer", references=refs), (result,), dataset)


def test_subperiod_answer_keeps_actual_scope(scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    args = dataset.scope.model_dump(mode="json") | {
        "period_start": "2026-08-01",
        "period_end": "2026-09-01",
    }
    result = Dispatcher(dataset).dispatch(TOOLS[0], json.dumps(args))
    ref = Reference(tool_result_id=result.tool_result_id, fact_id=result.facts[1].fact_id)
    run = send(
        ChatSession(
            dataset,
            Scripted(
                [
                    Turn(calls=(Call("august", TOOLS[0], json.dumps(args)),)),
                    Turn(content=reply(kind="answer", references=(ref,)).model_dump_json()),
                ]
            ),
        ),
        "Сколько денег в конце августа?",
    )
    assert run.error_code is None and run.facts[0].value == "500000.00"
    assert events(run, "tool_call")[0]["arguments"] == args
    assert str(run.tool_results[0].period_start) == "2026-08-01"
    assert str(run.tool_results[0].period_end) == "2026-09-01"


def test_full_report_requires_all_four_tools_even_with_valid_synthesis(scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    complete = send(ChatSession(dataset, MockChatProvider()), "Дай отчёт")
    provider = Scripted(
        [
            Turn(calls=(Call("only-one", TOOLS[0], dataset.scope.model_dump_json()),)),
            Turn(content=complete.reply.model_dump_json()),
            Turn(content=reply(kind="clarify").model_dump_json()),
        ]
    )
    run = send(ChatSession(dataset, provider), "Дай отчёт")
    assert run.report is None and run.reply.kind == "clarify"
    assert events(run, "grounding_rejected")[0]["reason_code"] == "MISSING_FULL_PERIOD_TOOLS"


def test_timeout_cancels_provider_attempts_and_returns_error(scenarios):
    class Slow:
        mode = "mock"
        model = None

        async def complete(self, *args):
            await asyncio.sleep(1)

    dataset = generate(scenarios, "cloudscale-ai", 42)
    run = send(
        ChatSession(dataset, Slow(), Budgets(attempt_timeout=0.001, retry_backoff=0)),
        "Какой runway?",
    )
    assert run.error_code == "PROVIDER_TIMEOUT"
    assert len(events(run, "provider_turn")) == 2 and not run.facts


def test_fact_data_mismatch_cannot_be_used_in_chat(scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    result = Dispatcher(dataset).dispatch(TOOLS[0], dataset.scope.model_dump_json())
    changed = result.facts[1].model_copy(update={"value": "999999.00"})
    corrupted = result.model_copy(update={"facts": (result.facts[0], changed, *result.facts[2:])})
    ref = Reference(tool_result_id=result.tool_result_id, fact_id=changed.fact_id)
    with pytest.raises(GroundingError, match="FACT_DATA_MISMATCH"):
        validate_reply(reply(kind="answer", references=(ref,)), (corrupted,), dataset)


def test_invalid_reply_repair_is_bounded_and_safe(scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    secret = "synthetic-hidden-content"
    bad = Turn(content=json.dumps({"kind": "help", secret: secret}))
    provider = Scripted([bad, bad])
    run = send(ChatSession(dataset, provider), "Привет")
    assert run.error_code == "INVALID_REPLY" and run.reply is None
    assert len(events(run, "grounding_rejected")) == 2
    assert provider.requests[-1][1] == []
    assert secret not in json.dumps(run.trace)
    assert secret not in provider.requests[-1][0][-1]["content"]


def test_repair_can_safely_clarify_instead_of_fabricating_answer(scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    fake = reply(kind="answer", references=(Reference(tool_result_id="fake", fact_id="fake"),))
    provider = Scripted(
        [
            Turn(content=fake.model_dump_json()),
            Turn(content=reply(kind="clarify").model_dump_json()),
        ]
    )
    run = send(ChatSession(dataset, provider), "Сколько денег?")
    assert run.error_code is None and run.reply.kind == "clarify"
    assert not run.facts and len(events(run, "plan_incomplete")) == 1


def test_failed_turn_does_not_poison_followup_tool_protocol(scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    provider = Scripted(
        [
            Turn(calls=(Call("pending", TOOLS[0], dataset.scope.model_dump_json()),)),
            ProviderFailure("AUTH_ERROR"),
            Turn(content=reply(kind="help").model_dump_json()),
        ]
    )
    session = ChatSession(dataset, provider)
    failed = send(session, "Какой runway?")
    assert failed.error_code == "AUTH_ERROR" and failed.report is None
    assert not any(m.get("tool_calls") for m in session.messages)
    recovered = send(session, "Привет")
    assert recovered.error_code is None
    assert session.results  # completed calculations remain reusable on the same snapshot


def test_unknown_tools_duplicate_ids_and_changed_money_are_rejected(scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    args = dataset.scope.model_dump(mode="json") | {"opening_balance": "999999.00"}
    provider = Scripted(
        [
            Turn(
                calls=(
                    Call("one", "Shell", "{}"),
                    Call("two", TOOLS[0], json.dumps(args)),
                    Call("two", TOOLS[0], dataset.scope.model_dump_json()),
                )
            ),
            Turn(content=reply(kind="clarify").model_dump_json()),
        ]
    )
    run = send(ChatSession(dataset, provider), "Поменяй баланс")
    assert all(e["status"] == "error" for e in events(run, "tool_result"))
    assert not run.facts and run.report is None
    assert "999999" not in json.dumps(run.trace)
    assert events(run, "tool_call")[0]["tool"] == "unknown"


def test_repeated_tool_calls_return_cache_hit(scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    provider = Scripted(
        [
            Turn(calls=(Call("first", TOOLS[0], dataset.scope.model_dump_json()),)),
            Turn(calls=(Call("second", TOOLS[0], dataset.scope.model_dump_json()),)),
            Turn(content=reply(kind="clarify").model_dump_json()),
        ]
    )
    run = send(ChatSession(dataset, provider), "Проверь")
    assert [e["cache_hit"] for e in events(run, "tool_result")] == [False, True]


@pytest.mark.parametrize(
    "budget,expected",
    [
        (Budgets(provider_turns=1), "TURN_LIMIT"),
        (Budgets(tool_calls=0), "TOOL_CALL_LIMIT"),
        (Budgets(deadline=0), "RUN_DEADLINE"),
    ],
)
def test_dialogue_budgets_return_visible_errors(budget, expected, scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    run = send(ChatSession(dataset, MockChatProvider(), budget), "Какой runway?")
    assert run.error_code == expected and run.report is None
    assert events(run, "run_finished")[-1]["status"] == "error"


def test_transient_retry_and_session_limit(scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    provider = Scripted(
        [
            ProviderFailure("PROVIDER_TIMEOUT", transient=True),
            Turn(content=reply(kind="help").model_dump_json()),
        ]
    )
    session = ChatSession(dataset, provider, Budgets(retry_backoff=0))
    assert send(session, "Привет").error_code is None
    assert len(provider.requests) == 3  # transient retry, reply, semantic review
    session.turn_count = MAX_SESSION_TURNS
    with pytest.raises(ValueError, match="Session limit"):
        send(session, "Ещё")


def test_live_gateway_dialogue_transport_with_history_no_real_api(scenarios, monkeypatch):
    secret = "synthetic-chat-gateway-key"
    monkeypatch.setenv("LLM_MODEL", "litellm_proxy/synthetic-model")
    monkeypatch.setenv("LITELLM_PROXY_API_BASE", "https://gateway.test/v1")
    monkeypatch.setenv("LITELLM_PROXY_API_KEY", secret)
    mock = MockChatProvider()
    requests = []

    async def acompletion(**kwargs):
        requests.append(kwargs)
        turn = await mock.complete(kwargs["messages"], kwargs.get("tools", []), kwargs["timeout"])
        return {"choices": [{"message": turn.message()}]}

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=acompletion))
    dataset = generate(scenarios, "cloudscale-ai", 42)
    session = ChatSession(dataset, LiveProvider())
    first = send(session, "Какой runway?")
    second = send(session, "Почему?")
    assert first.facts == second.facts and second.error_code is None
    assert all(
        r["api_key"] == secret and r["api_base"] == "https://gateway.test/v1" for r in requests
    )
    dialogue = [r for r in requests if not r["messages"][0]["content"].startswith(REVIEW_PROMPT)]
    final_requests = [r for r in dialogue if not r.get("tools")]
    assert len(final_requests) == 2
    assert all(r["response_format"] == {"type": "json_object"} for r in final_requests)
    assert all("tool_choice" not in r for r in final_requests)
    assert any(
        m["content"] == "Какой runway?" for m in dialogue[-1]["messages"] if m["role"] == "user"
    )
    reviews = [r for r in requests if r["messages"][0]["content"].startswith(REVIEW_PROMPT)]
    assert reviews and all(r["response_format"] == {"type": "json_object"} for r in reviews)
    assert secret not in json.dumps(first.trace + second.trace)


def test_console_end_to_end_visible_calls_reports_and_reset(tmp_path, monkeypatch, capsys):
    inputs = iter(["Привет", "Какой runway?", "Дай отчёт", "/reset", "Какой runway?", "/exit"])
    monkeypatch.setattr(builtins, "input", lambda _: next(inputs))
    original_import = builtins.__import__

    def no_sdk(name, *args, **kwargs):
        if name == "litellm" or name.startswith("litellm."):
            raise AssertionError("Mock chat must not import SDK")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_sdk)
    assert main(["chat", "--company", "cloudscale-ai", "--output", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "MOCK" in output and "Смотрю денежные потоки" in output and "270\u00a0000,00" in output
    assert "История и кеш очищены" in output
    reports = list(tmp_path.glob("*/report.json"))
    assert len(reports) == 1
    raw = json.loads(reports[0].read_text())
    assert raw["status"] == "complete"
    assert len(list(tmp_path.glob("*/answer.json"))) == 2
    trace = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert len([e for e in trace if e["event"] == "tool_call" and e["tool"] == TOOLS[0]]) == 2


def test_console_live_config_failure_before_creating_output(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    target = tmp_path / "new"
    assert (
        main(["chat", "--company", "cloudscale-ai", "--provider", "live", "--output", str(target)])
        == 2
    )
    assert not target.exists()


@pytest.mark.parametrize("cid", ["apex-logistics", "cloudscale-ai", "harbour-trade"])
@pytest.mark.parametrize(
    "question,expected",
    [
        ("моежешь мне дать сначала расходы, а потом аномалии?", (TOOLS[1], TOOLS[2])),
        ("Сначала аномалии, затем расходы", (TOOLS[2], TOOLS[1])),
    ],
)
def test_compound_requests_keep_all_parts_and_requested_order(cid, question, expected, scenarios):
    dataset = generate(scenarios, cid, 42)
    run = send(ChatSession(dataset, MockChatProvider()), question)
    assert run.error_code is None and run.reply.kind == "answer"
    assert tuple(e["tool"] for e in events(run, "tool_call")) == expected
    by_result = {r.tool_result_id: r.tool_name for r in run.tool_results}
    cited = tuple(dict.fromkeys(by_result[f.tool_result_id] for f in run.facts))
    assert cited == expected
    assert events(run, "narrative_review")[-1]["supported"] is True
    text = render_reply(run, dataset)
    assert "{{" not in text and "result:" not in text and "|---" not in text


def test_premature_answer_cannot_skip_pending_anomaly_task(scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    dispatcher = Dispatcher(dataset)
    expense = dispatcher.dispatch(TOOLS[1], dataset.scope.model_dump_json())
    anomaly = dispatcher.dispatch(TOOLS[2], dataset.scope.model_dump_json())
    refs = (
        Reference(tool_result_id=expense.tool_result_id, fact_id=expense.facts[0].fact_id),
        Reference(tool_result_id=anomaly.tool_result_id, fact_id=anomaly.facts[-2].fact_id),
    )
    plan = AnalysisPlan(
        steps=tuple(
            AnalysisStep(task="Проверить данные", tool=n, scope=dataset.scope)
            for n in (TOOLS[1], TOOLS[2])
        )
    )
    provider = Scripted(
        [
            Turn(
                calls=(
                    Call("goal", PLAN_TOOL, plan.model_dump_json()),
                    Call("expenses", TOOLS[1], dataset.scope.model_dump_json()),
                )
            ),
            Turn(content=reply(kind="answer", references=refs[:1]).model_dump_json()),
            Turn(calls=(Call("anomalies", TOOLS[2], dataset.scope.model_dump_json()),)),
            Turn(content=reply(kind="answer", references=refs).model_dump_json()),
        ]
    )
    run = send(ChatSession(dataset, provider), "Сначала расходы, затем аномалии")
    assert run.error_code is None
    assert [e["tool"] for e in events(run, "tool_call")] == [TOOLS[1], TOOLS[2]]
    assert len(events(run, "plan_incomplete")) == 1
    assert (
        len(events(run, "narrative_review")) == 1
    )  # incomplete answer was never reviewed/published
    assert run.facts == (expense.facts[0], anomaly.facts[-2])


def test_completed_tools_still_require_both_topics_in_final_answer(scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    dispatcher = Dispatcher(dataset)
    expense = dispatcher.dispatch(TOOLS[1], dataset.scope.model_dump_json())
    anomaly = dispatcher.dispatch(TOOLS[2], dataset.scope.model_dump_json())
    refs = (
        Reference(tool_result_id=expense.tool_result_id, fact_id=expense.facts[0].fact_id),
        Reference(tool_result_id=anomaly.tool_result_id, fact_id=anomaly.facts[-2].fact_id),
    )
    provider = Scripted(
        [
            Turn(
                calls=(
                    Call("expenses", TOOLS[1], dataset.scope.model_dump_json()),
                    Call("anomalies", TOOLS[2], dataset.scope.model_dump_json()),
                )
            ),
            Turn(content=reply(kind="answer", references=refs[:1]).model_dump_json()),
            Turn(content=reply(kind="answer", references=refs).model_dump_json()),
        ]
    )
    run = send(ChatSession(dataset, provider), "Расходы и аномалии")
    assert run.error_code is None and len(run.facts) == 2
    assert events(run, "grounding_rejected")[0]["reason_code"] == "INCOMPLETE_PLAN_ANSWER"


@pytest.mark.parametrize(
    "text,reason",
    [
        ("Осталось 999999 HKD", "UNCITED_NUMBER"),
        ("Осталось пять миллионов", "UNCITED_NUMBER"),
        ("Runway равен Ⅲ месяцам", "UNCITED_NUMBER"),
        ("Остаток {{fact:5}}", "INVALID_FACT_PLACEHOLDER"),
        ("Остаток {{value}}", "INVALID_FACT_PLACEHOLDER"),
        ("Все хорошо", "UNUSED_FACT_REFERENCE"),
        ("\u001b[2JОстаток {{fact:0}}", "UNSAFE_CHAT_TEXT"),
    ],
)
def test_free_numbers_invalid_placeholders_and_controls_are_blocked(text, reason, scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    result = Dispatcher(dataset).dispatch(TOOLS[0], dataset.scope.model_dump_json())
    ref = Reference(tool_result_id=result.tool_result_id, fact_id=result.facts[1].fact_id)
    candidate = ChatReply(kind="answer", text=text, references=(ref,))
    with pytest.raises(GroundingError, match=reason):
        validate_reply(candidate, (result,), dataset)


def test_unsupported_prose_is_reviewed_and_repaired_before_display(scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    result = Dispatcher(dataset).dispatch(TOOLS[0], dataset.scope.model_dump_json())
    ref = Reference(tool_result_id=result.tool_result_id, fact_id=result.facts[1].fact_id)
    provider = Scripted(
        [
            Turn(calls=(Call("cash", TOOLS[0], dataset.scope.model_dump_json()),)),
            Turn(
                content=ChatReply(
                    kind="answer",
                    text="Остаток {{fact:0}} доказывает мошенничество.",
                    references=(ref,),
                ).model_dump_json()
            ),
            Turn(
                content=ChatReply(
                    kind="answer",
                    text="Остаток денег — {{fact:0}}. "
                    "Сам по себе остаток не объясняет причины платежей.",
                    references=(ref,),
                ).model_dump_json()
            ),
        ]
    )
    provider.reviews = [
        NarrativeReview(supported=False, issues=("unsupported_cause",)),
        NarrativeReview(supported=True),
    ]
    run = send(ChatSession(dataset, provider), "Проверь остаток")
    assert run.error_code is None
    assert "мошенничество" not in render_reply(run, dataset)
    assert [e["supported"] for e in events(run, "narrative_review")] == [False, True]
    assert events(run, "grounding_rejected")[0]["review_issues"] == ["unsupported_cause"]
    assert "мошенничество" not in json.dumps(run.trace, ensure_ascii=False)


def test_repeated_unsupported_text_never_becomes_answer(scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    result = Dispatcher(dataset).dispatch(TOOLS[0], dataset.scope.model_dump_json())
    ref = Reference(tool_result_id=result.tool_result_id, fact_id=result.facts[1].fact_id)
    candidate = ChatReply(
        kind="answer", text="Остаток {{fact:0}} гарантирует безопасность.", references=(ref,)
    )
    provider = Scripted(
        [
            Turn(calls=(Call("cash", TOOLS[0], dataset.scope.model_dump_json()),)),
            Turn(content=candidate.model_dump_json()),
            Turn(content=candidate.model_dump_json()),
        ]
    )
    provider.reviews = [NarrativeReview(supported=False, issues=("unsupported_claim",))] * 2
    run = send(ChatSession(dataset, provider), "Проверь остаток")
    assert run.error_code == "INVALID_REPLY" and run.reply is None and not run.facts
    assert "гарантирует" not in render_reply(run, dataset)


def test_plan_cannot_drop_tasks_and_wrong_order_tool_is_rejected(scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    plan = AnalysisPlan(
        steps=tuple(
            AnalysisStep(task="Проверить данные", tool=name, scope=dataset.scope)
            for name in (TOOLS[1], TOOLS[2])
        )
    )
    shortened = AnalysisPlan(steps=plan.steps[:1])
    provider = Scripted(
        [
            Turn(
                calls=(
                    Call("goal", PLAN_TOOL, plan.model_dump_json()),
                    Call("out-of-order", TOOLS[2], dataset.scope.model_dump_json()),
                )
            ),
            Turn(calls=(Call("drop-task", PLAN_TOOL, shortened.model_dump_json()),)),
            Turn(content=reply(kind="clarify").model_dump_json()),
        ]
    )
    run = send(ChatSession(dataset, provider), "Сначала расходы, потом аномалии")
    assert run.plan == plan and not run.tool_results
    assert any(e.get("error_code") == "INVALID_PLAN" for e in run.trace)
    assert events(run, "run_finished")[-1]["status"] == "needs_input"


def test_semantic_review_can_force_agent_to_extend_incomplete_goal(scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    dispatcher = Dispatcher(dataset)
    expense = dispatcher.dispatch(TOOLS[1], dataset.scope.model_dump_json())
    anomaly = dispatcher.dispatch(TOOLS[2], dataset.scope.model_dump_json())
    refs = (
        Reference(tool_result_id=expense.tool_result_id, fact_id=expense.facts[0].fact_id),
        Reference(tool_result_id=anomaly.tool_result_id, fact_id=anomaly.facts[-2].fact_id),
    )
    initial = AnalysisPlan(
        steps=(AnalysisStep(task="Расходы", tool=TOOLS[1], scope=dataset.scope),)
    )
    extended = AnalysisPlan(
        steps=(*initial.steps, AnalysisStep(task="Аномалии", tool=TOOLS[2], scope=dataset.scope))
    )
    provider = Scripted(
        [
            Turn(calls=(Call("initial", PLAN_TOOL, initial.model_dump_json()),)),
            Turn(calls=(Call("expenses", TOOLS[1], dataset.scope.model_dump_json()),)),
            Turn(content=reply(kind="answer", references=refs[:1]).model_dump_json()),
            Turn(calls=(Call("extend", PLAN_TOOL, extended.model_dump_json()),)),
            Turn(calls=(Call("anomalies", TOOLS[2], dataset.scope.model_dump_json()),)),
            Turn(content=reply(kind="answer", references=refs).model_dump_json()),
        ]
    )
    provider.reviews = [
        NarrativeReview(supported=False, issues=("missing_requested_part",)),
        NarrativeReview(supported=True),
    ]
    run = send(ChatSession(dataset, provider), "Сначала расходы, потом аномалии")
    assert run.error_code is None and run.plan == extended and len(run.facts) == 2
    assert [e["tool"] for e in events(run, "tool_call")] == [TOOLS[1], TOOLS[2]]
    assert len(events(run, "plan_updated")) == 2
    assert len(events(run, "provider_turn")) == 8  # review also consumes the turn budget


def test_invalid_semantic_review_cannot_publish_financial_answer(scenarios):
    class BadReview(MockChatProvider):
        async def complete(self, messages, tools, timeout):
            if messages[0]["content"].startswith(REVIEW_PROMPT):
                return Turn(content='{"supported":"yes","issues":[]}')
            return await super().complete(messages, tools, timeout)

    dataset = generate(scenarios, "cloudscale-ai", 42)
    run = send(ChatSession(dataset, BadReview()), "Какой runway?")
    assert run.error_code == "INVALID_REVIEW" and run.reply is None and not run.facts


def test_verbose_is_optional_and_report_has_model_prose_plus_audit(tmp_path, monkeypatch, capsys):
    inputs = iter(["моежешь мне дать сначала расходы, а потом аномалии?", "Дай отчёт", "/exit"])
    monkeypatch.setattr(builtins, "input", lambda _: next(inputs))
    assert main(["chat", "--company", "cloudscale-ai", "--output", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "Разбираю расходы" in output and "Проверяю аномалии" in output
    assert "Начну с расходов" in output and "Теперь аномалии" in output
    assert "result:" not in output and '"company_id"' not in output
    report_file = next(tmp_path.glob("*/report.json"))
    narrative = json.loads(report_file.with_name("reply.json").read_text())
    assert narrative["text"] and narrative["references"]
    text = report_file.with_suffix(".md").read_text()
    assert text.startswith("По прошлым денежным потокам") and "## Налоговый драфт" in text
    inputs = iter(["Покажи расходы", "/exit"])
    monkeypatch.setattr(builtins, "input", lambda _: next(inputs))
    assert (
        main(
            [
                "chat",
                "--company",
                "cloudscale-ai",
                "--verbose",
                "--output",
                str(tmp_path / "verbose"),
            ]
        )
        == 0
    )
    verbose = capsys.readouterr().out
    assert "CategorizeOpex" in verbose and '"company_id"' in verbose


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"kind": "answer"}, "answer_requires_references"),
        ({"kind": "report"}, "report_requires_synthesis"),
        ({"kind": "method"}, "method_required"),
        ({"kind": "help", "method": "runway"}, "method_forbidden"),
        (
            {"kind": "help", "references": [{"tool_result_id": "test:r", "fact_id": "test:f"}]},
            "nonfinancial_forbids_evidence",
        ),
    ],
)
def test_chat_invariant_diagnostics_identify_the_actual_payload_error(payload, code):
    with pytest.raises(ValidationError) as raised:
        ChatReply.model_validate({"text": "Уточни запрос.", **payload})
    diagnostic = synthesis_diagnostic(raised.value, schema_label="ChatReply")
    assert diagnostic["validation_errors"][0]["code"] == code
    assert "finding_code" not in diagnostic["validation_errors"][0]["message"]


@pytest.mark.parametrize("duplicate", [False, True])
def test_answer_synthesis_rejection_gives_correct_feedback_and_repairs(scenarios, duplicate):
    class Confused(MockChatProvider):
        bad_sent = False
        feedback = None

        async def complete(self, messages, tools, timeout):
            for message in messages:
                if (message.get("content") or "").startswith("Reply rejected."):
                    self.feedback, _ = json.JSONDecoder().raw_decode(
                        message["content"].split("Feedback:\n")[1]
                    )
            turn = await super().complete(messages, tools, timeout)
            if (
                not turn.calls
                and not self.bad_sent
                and not messages[0]["content"].startswith(REVIEW_PROMPT)
            ):
                self.bad_sent = True
                draft = ComposedReply.model_validate_json(turn.content)
                results = tuple(
                    Dispatcher(dataset).dispatch(name, dataset.scope.model_dump_json())
                    for name in TOOLS[1:3]
                )
                raw = canonical_composed(draft, fact_aliases(results), dataset)
                insight = {
                    "finding_code": "opex_concentration",
                    "action_code": "review_operating_spend",
                    "evidence": raw["references"][:1],
                }
                raw["synthesis"] = {"insights": [insight, insight] if duplicate else [insight]}
                return Turn(content=json.dumps(raw))
            return turn

    dataset = generate(scenarios, "cloudscale-ai", 42)
    provider = Confused()
    run = send(ChatSession(dataset, provider), "Сначала расходы, потом аномалии")
    assert run.error_code is None and run.reply.kind == "answer" and run.reply.synthesis is None
    expected = "duplicate_findings" if duplicate else "answer_forbids_synthesis"
    assert provider.feedback["validation_errors"][0]["code"] == expected
    message = provider.feedback["validation_errors"][0]["message"]
    assert "once in insights" in message if duplicate else "insights=[]" in message
    assert "synthesis=null" not in message
    assert [e["tool"] for e in events(run, "tool_call")] == [TOOLS[1], TOOLS[2]]


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("duplicate", "duplicate_plan_steps"),
        ("report", "report_plan_requires_tools"),
        ("scope", "invalid_plan_scope"),
        ("title", "UNCITED_NUMBER"),
        ("extra", "extra_forbidden"),
    ],
)
def test_invalid_plan_has_specific_safe_trace_and_tool_feedback(mutation, expected, scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    secret = "synthetic-untrusted-field"
    step = {"task": "Расходы", "tool": TOOLS[1], "scope": dataset.scope.model_dump(mode="json")}
    raw = {"mode": "answer", "steps": [step]}
    if mutation == "duplicate":
        raw["steps"].append(step)
    elif mutation == "report":
        raw["mode"] = "report"
    elif mutation == "scope":
        step["scope"]["company_id"] = secret
    elif mutation == "title":
        step["task"] = "Расходы за 2026"
    elif mutation == "extra":
        step[secret] = secret
    provider = Scripted(
        [
            Turn(calls=(Call("bad-plan", PLAN_TOOL, json.dumps(raw)),)),
            Turn(content=reply(kind="clarify").model_dump_json()),
        ]
    )
    run = send(ChatSession(dataset, provider), "Покажи расходы")
    rejected = next(e for e in run.trace if e.get("error_code") == "INVALID_PLAN")
    assert rejected["validation_errors"][0]["code"] == expected
    feedback = json.loads(
        next(m["content"] for m in provider.requests[1][0] if m["role"] == "tool")
    )["error"]["feedback"]
    assert feedback["validation_errors"] == rejected["validation_errors"]
    assert secret not in json.dumps(run.trace) + json.dumps(feedback)
    assert run.reply.kind == "clarify" and run.plan is None and not run.tool_results


@pytest.mark.parametrize("cid", ["apex-logistics", "cloudscale-ai", "harbour-trade"])
def test_sequential_tools_keep_one_catalogue_and_preserve_protocol_and_full_audit(cid, scenarios):
    class Sequential(MockChatProvider):
        requests = None

        def __init__(self):
            super().__init__()
            self.requests = []

        async def complete(self, messages, tools, timeout):
            self.requests.append((json.loads(json.dumps(messages)), tools))
            turn = await super().complete(messages, tools, timeout)
            if turn.calls and turn.calls[0].name in TOOLS:
                turn = Turn(calls=turn.calls[:1])
                original = turn.message() | {"reasoning_content": "synthetic-provider-reasoning"}
                return Turn(calls=turn.calls, assistant_message=original)
            return turn

    dataset = generate(scenarios, cid, 42)
    provider = Sequential()
    run = send(ChatSession(dataset, provider), "Дай полный отчёт")
    assert run.error_code is None and run.report.status == "complete"
    for messages, _ in provider.requests:
        evidence = [m for m in messages if (m.get("content") or "").startswith(EVIDENCE_PREFIX)]
        assert len(evidence) <= 1
        outstanding = set()
        for m in messages:
            if m.get("tool_calls"):
                outstanding.update(c["id"] for c in m["tool_calls"])
            if m["role"] == "tool":
                assert m["tool_call_id"] in outstanding
                outstanding.remove(m["tool_call_id"])
        assert not outstanding
    messages, tools = provider.requests[-2]  # final response, then isolated review
    assert tools == []
    assert not any(m.get("reasoning_content") for m in messages)
    assert any(
        sum(m.get("reasoning_content") is not None for m in history) == len(TOOLS) - 1
        for history, available_tools in provider.requests
        if available_tools
    )
    evidence = json.loads(messages[-1]["content"][len(EVIDENCE_PREFIX) :])
    assert evidence["plan_mode"] == "report" and evidence["report_options"]["finding_options"]
    complete = {r.tool_name: r for r in run.tool_results}
    aliases = fact_aliases(run.tool_results)
    shortened = []
    for projected in evidence["results"]:
        real = complete[projected["tool_name"]]
        for fact in projected["facts"]:
            if fact.get("value_in_audit"):
                shortened.append(fact)
                assert len(aliases[fact["ref"]].value) > 8 and "value" not in fact
            else:
                assert fact["value"] == aliases[fact["ref"]].model_dump(mode="json")["value"]
        assert real == Dispatcher(dataset).dispatch(real.tool_name, dataset.scope.model_dump_json())
    assert shortened


def test_answer_context_omits_report_options_and_console_explains_failed_reply(scenarios):
    dataset = generate(scenarios, "cloudscale-ai", 42)
    result = Dispatcher(dataset).dispatch(TOOLS[1], dataset.scope.model_dump_json())
    evidence = chat_evidence((result,), dataset)
    assert evidence["report_options"] == {"finding_options": []}
    provider = Scripted(
        [
            Turn(calls=(Call("expense", TOOLS[1], dataset.scope.model_dump_json()),)),
            Turn(content="bad JSON"),
            Turn(content="bad JSON"),
        ]
    )
    run = send(ChatSession(dataset, provider), "Покажи расходы")
    text = render_reply(run, dataset)
    assert "некорректный JSON" in text and "сохранены в кеше" in text and "INVALID_REPLY" in text
    assert run.reply is None and not run.facts

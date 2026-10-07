from __future__ import annotations

import asyncio
import copy
import json
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from fintech_copilot.domain.finance import anomalies, liquidity
from fintech_copilot.domain.generator import generate
from fintech_copilot.domain.models import AnomalyConfig, Dataset, Scope
from fintech_copilot.orchestration.agent import run_agent
from fintech_copilot.orchestration.chat_reply import fact_aliases, model_evidence
from fintech_copilot.orchestration.context import model_tool_result, synthesis_options
from fintech_copilot.orchestration.diagnostics import synthesis_diagnostic
from fintech_copilot.orchestration.grounding import GroundingError, options, validate_synthesis
from fintech_copilot.orchestration.models import Synthesis
from fintech_copilot.orchestration.providers import MockProvider, Turn
from fintech_copilot.render import markdown
from fintech_copilot.tools.dispatcher import TOOLS, Dispatcher


def changed(dataset, **updates):
    return Dataset.model_validate(dataset.model_dump(mode="json") | updates)


def test_safe_average_runway_preserves_stress_warning_in_batch(golden, validator):
    dataset = changed(golden["cloudscale-ai"], opening_balance="750000.00")
    result = liquidity(dataset, dataset.scope)
    assert result.runway_months == Decimal("3.82") and not result.low_runway
    assert result.last_month_stress_runway_months == Decimal("2.00")
    assert result.last_month_stress_low_runway and not result.cash_gap_intervals

    output = asyncio.run(run_agent(dataset, "Liquidity report", MockProvider()))
    assert output.report.status == "complete"
    validator("report.schema.json").validate(output.report.model_dump(mode="json"))
    findings = {i.finding_code: i for i in output.report.synthesis.insights}
    assert "low_runway" not in findings and "stress_low_runway" in findings
    guidance = synthesis_options(output.report.tool_results, dataset.scope)
    assert guidance["required_findings"] == ["stress_low_runway"]
    tool = next(r for r in output.report.tool_results if r.tool_name == TOOLS[0])
    wire = json.loads(model_tool_result(tool))
    metrics = {f["metric"]: f["value"] for f in wire["facts"]}
    assert metrics["last_month_stress_runway_months"] == "2.00"
    assert metrics["last_month_net_burn"] == "210000.00"
    assert metrics["last_month_stress_low_runway"] is True
    by_id = {f.fact_id: f.metric for f in tool.facts}
    assert {by_id[r.fact_id] for r in findings["stress_low_runway"].evidence} == {
        "last_month_stress_runway_months",
        "closing_cash",
        "last_month_net_burn",
        "last_month_stress_low_runway",
    }
    text = markdown(output.report)
    assert "Риск ликвидности при повторении последнего месяца" in text
    assert "2.00 months" in text


def test_missing_stress_warning_requires_repair_and_cannot_publish(golden):
    dataset = changed(golden["cloudscale-ai"], opening_balance="750000.00")

    class SkipStress(MockProvider):
        async def complete(self, messages, tools, timeout):
            turn = await super().complete(messages, tools, timeout)
            if turn.calls:
                return turn
            raw = json.loads(turn.content)
            raw["insights"] = [
                i for i in raw["insights"] if i["finding_code"] != "stress_low_runway"
            ]
            return Turn(content=json.dumps(raw))

    output = asyncio.run(run_agent(dataset, "Liquidity report", SkipStress()))
    assert output.report.status == "partial" and output.report.synthesis is None
    events = [e for e in output.trace if e["event"] == "grounding_rejected"]
    assert len(events) == 2
    assert all(e["reason_code"] == "MISSING_STRESS_RUNWAY_FINDING" for e in events)
    results = output.report.tool_results
    raw = {
        "insights": [
            {
                "finding_code": "opex_concentration",
                "action_code": "review_operating_spend",
                "evidence": [r.model_dump() for r in options(results)["opex_concentration"][0]],
            }
        ]
    }
    with pytest.raises(GroundingError, match="MISSING_STRESS_RUNWAY_FINDING") as rejected:
        validate_synthesis(Synthesis.model_validate(raw), results, dataset)
    diagnostic = synthesis_diagnostic(rejected.value)
    assert "stress_low_runway" in diagnostic["validation_errors"][0]["message"]
    assert diagnostic["validation_errors"][0]["expected_evidence"]


def test_stress_only_risk_is_not_reported_as_no_material_findings(golden):
    base = golden["harbour-trade"]
    rent = next(t.model_dump(mode="json") for t in base.transactions if t.category == "rent")
    receipt = next(t.model_dump(mode="json") for t in base.transactions if t.direction == "credit")
    rows = [
        rent
        | {
            "transaction_id": f"stress:{month}:{category}",
            "category": category,
            "amount": "80000.00",
            "posted_on": f"2026-{month:02d}-10",
        }
        for month in (1, 2, 3)
        for category in ("rent", "fuel", "logistics")
    ]
    rows += [
        receipt
        | {
            "transaction_id": f"receipt:{month}",
            "amount": "240000.00",
            "posted_on": f"2026-{month:02d}-20",
        }
        for month in (1, 2)
    ]
    dataset = changed(base, transactions=rows, opening_balance="720000.00")
    result = liquidity(dataset, dataset.scope)
    assert result.runway_months == Decimal("6.00") and not result.low_runway
    assert result.last_month_stress_runway_months == Decimal("2.00")
    dispatcher = Dispatcher(dataset)
    results = tuple(dispatcher.dispatch(name, dataset.scope.model_dump_json()) for name in TOOLS)
    assert set(options(results)) == {"stress_low_runway"}
    output = asyncio.run(run_agent(dataset, "Full report", MockProvider()))
    assert output.report.status == "complete"
    assert [i.finding_code for i in output.report.synthesis.insights] == ["stress_low_runway"]


def test_missing_stress_warning_can_be_repaired_from_exact_evidence(golden):
    class SkipOnce(MockProvider):
        skipped = False

        async def complete(self, messages, tools, timeout):
            turn = await super().complete(messages, tools, timeout)
            if turn.calls or self.skipped:
                return turn
            self.skipped = True
            raw = json.loads(turn.content)
            raw["insights"] = [
                i for i in raw["insights"] if i["finding_code"] != "stress_low_runway"
            ]
            return Turn(content=json.dumps(raw))

    output = asyncio.run(run_agent(golden["cloudscale-ai"], "Liquidity report", SkipOnce()))
    assert output.report.status == "complete"
    events = [e for e in output.trace if e["event"] == "grounding_rejected"]
    assert len(events) == 1 and events[0]["reason_code"] == "MISSING_STRESS_RUNWAY_FINDING"
    validate_synthesis(output.report.synthesis, output.report.tool_results, golden["cloudscale-ai"])


@pytest.mark.parametrize("opening,expected_flag", [("959999.99", True), ("960000.00", False)])
def test_stress_warning_uses_unrounded_tool_flag(golden, opening, expected_flag):
    dataset = changed(golden["cloudscale-ai"], opening_balance=opening)
    result = Dispatcher(dataset).dispatch(TOOLS[0], dataset.scope.model_dump_json())
    assert result.data.last_month_stress_runway_months == Decimal("3.00")
    assert result.data.last_month_stress_low_runway is expected_flag
    assert not result.data.low_runway
    assert ("stress_low_runway" in options((result,))) is expected_flag


def test_chat_projection_keeps_required_stress_finding(golden):
    from fintech_copilot.orchestration.chat import chat_evidence

    dataset = changed(golden["cloudscale-ai"], opening_balance="750000.00")
    dispatcher = Dispatcher(dataset)
    results = tuple(dispatcher.dispatch(name, dataset.scope.model_dump_json()) for name in TOOLS)
    aliases = fact_aliases(results)
    projected = model_evidence(chat_evidence(results, dataset, include_report=True), aliases)
    assert projected["report_options"]["required_findings"] == ["stress_low_runway"]
    stress = next(
        o
        for o in projected["report_options"]["finding_options"]
        if o["finding_code"] == "stress_low_runway"
    )
    assert {aliases[a].metric for a in stress["evidence_bundles"][0]} == {
        "last_month_stress_runway_months",
        "closing_cash",
        "last_month_net_burn",
        "last_month_stress_low_runway",
    }


@pytest.mark.parametrize("cid", ["apex-logistics", "harbour-trade"])
def test_safe_stress_does_not_create_required_warning(golden, cid):
    dataset = golden[cid]
    result = Dispatcher(dataset).dispatch(TOOLS[0], dataset.scope.model_dump_json())
    assert not result.data.last_month_stress_low_runway
    assert "stress_low_runway" not in options((result,))
    assert synthesis_options((result,), dataset.scope)["required_findings"] == []


@pytest.mark.parametrize(
    "setting,value,absent,present",
    [
        ("transaction_ratio", "100.00", "transaction_outlier", "monthly_spend_jump"),
        ("transaction_min_delta", "200000.00", "transaction_outlier", "monthly_spend_jump"),
        ("monthly_jump_ratio", "100.00", "monthly_spend_jump", "transaction_outlier"),
        ("monthly_jump_min_delta", "200000.00", "monthly_spend_jump", "transaction_outlier"),
        ("minimum_baseline_count", 11, "transaction_outlier", "monthly_spend_jump"),
    ],
)
def test_anomaly_settings_change_actual_tool_findings(golden, setting, value, absent, present):
    base = golden["cloudscale-ai"]
    config = base.anomaly_config.model_dump(mode="json") | {setting: value}
    dataset = changed(base, anomaly_config=config)
    result = Dispatcher(dataset).dispatch(TOOLS[2], dataset.scope.model_dump_json())
    assert result.status == "ok"
    kinds = {f.kind for f in result.data.findings}
    assert absent not in kinds and present in kinds
    original = Dispatcher(base).dispatch(TOOLS[2], base.scope.model_dump_json())
    assert original.tool_result_id != result.tool_result_id


def test_configured_lookback_is_causal_and_stable_for_subperiod(golden):
    base = golden["cloudscale-ai"]
    dataset = changed(base, anomaly_config={"baseline_lookback_months": 1})
    output, _ = anomalies(dataset, dataset.scope)
    outlier = next(f for f in output.findings if f.kind == "transaction_outlier")
    assert len(outlier.baseline_transaction_ids) == 5
    rows = {tx.transaction_id: tx for tx in dataset.transactions}
    assert all(rows[i].posted_on.month == 2 for i in outlier.baseline_transaction_ids)
    scope = Scope(**(dataset.scope.model_dump() | {"period_start": date(2026, 3, 1)}))
    assert anomalies(dataset, scope)[0].findings == output.findings
    strict = changed(
        dataset, anomaly_config={"baseline_lookback_months": 1, "minimum_baseline_count": 6}
    )
    signals, warnings = anomalies(strict, strict.scope)
    assert "transaction_outlier" not in {f.kind for f in signals.findings}
    assert "INSUFFICIENT_BASELINE:2026-03:rd_contractors" in warnings


def test_generated_snapshot_persists_config_and_replays_detector(scenarios, validator):
    original = generate(scenarios, "cloudscale-ai", 42)
    config = copy.deepcopy(scenarios)
    config["anomaly_config"].update(transaction_ratio="1000.00", monthly_jump_ratio="1000.00")
    updated = generate(config, "cloudscale-ai", 42)
    assert updated.transactions == original.transactions
    assert updated.snapshot_id != original.snapshot_id
    assert updated == generate(config, "cloudscale-ai", 42)
    restored = Dataset.model_validate_json(updated.model_dump_json())
    validator("dataset.schema.json").validate(restored.model_dump(mode="json"))
    result = Dispatcher(restored).dispatch(TOOLS[2], restored.scope.model_dump_json())
    assert result.status == "ok" and result.data.findings == ()
    assert result == Dispatcher(updated).dispatch(TOOLS[2], updated.scope.model_dump_json())
    assert anomalies(original, original.scope)[0].findings


def test_config_does_not_disable_cross_border_review_or_new_spend(golden):
    base = golden["harbour-trade"]
    config = {
        "transaction_ratio": "1000.00",
        "monthly_jump_ratio": "1000.00",
        "transaction_min_delta": "999999999.00",
        "monthly_jump_min_delta": "999999999.00",
    }
    updated = changed(base, anomaly_config=config)
    result, _ = anomalies(updated, updated.scope)
    assert result.unclassified_cross_border_total == Decimal("15000.00")
    assert len([f for f in result.findings if f.kind == "unclassified_cross_border"]) == 3
    debit = next(t.model_dump(mode="json") for t in base.transactions if t.category == "rent")
    debit["posted_on"] = "2026-02-10"
    updated = changed(updated, transactions=[debit])
    result, _ = anomalies(updated, updated.scope)
    assert [f.kind for f in result.findings] == ["new_spend"]


@pytest.mark.parametrize(
    "config",
    [
        {"minimum_baseline_count": 0},
        {"minimum_baseline_count": True},
        {"minimum_baseline_count": "5"},
        {"baseline_lookback_months": 0},
        {"baseline_lookback_months": 1.5},
        {"transaction_ratio": "0.99"},
        {"transaction_ratio": 3.0},
        {"transaction_ratio": "NaN"},
        {"monthly_jump_ratio": "Infinity"},
        {"transaction_min_delta": "-0.01"},
        {"monthly_jump_min_delta": 10000.0},
        {"threshold": "100.00"},
        None,
    ],
)
def test_invalid_anomaly_config_rejected_before_generation(scenarios, config):
    scenarios["anomaly_config"] = config
    with pytest.raises(ValidationError):
        generate(scenarios, "cloudscale-ai", 42)


def test_legacy_dataset_uses_frozen_default_detection_config(golden):
    raw = golden["cloudscale-ai"].model_dump(mode="json")
    raw.pop("anomaly_config")
    dataset = Dataset.model_validate(raw)
    assert dataset.anomaly_config == AnomalyConfig()
    assert anomalies(dataset, dataset.scope) == anomalies(golden["cloudscale-ai"], dataset.scope)
    with pytest.raises(ValidationError):
        dataset.anomaly_config.transaction_ratio = Decimal("100.00")

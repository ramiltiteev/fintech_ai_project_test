from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from test_orchestration import Scripted, run

from fintech_copilot.domain.finance import anomalies, categorize, liquidity
from fintech_copilot.domain.models import Dataset
from fintech_copilot.domain.tax import draft_tax
from fintech_copilot.orchestration.agent import Budgets
from fintech_copilot.orchestration.providers import Call, Turn
from fintech_copilot.tools.dispatcher import TOOLS


def test_positive_burn_with_depleted_cash_and_new_spend(golden):
    raw = golden["harbour-trade"].model_dump(mode="json")
    debit = next(t for t in raw["transactions"] if t["category"] == "rent")
    debit.update(posted_on="2026-02-10", amount="10.00")
    raw.update(transactions=[debit], opening_balance="5.00")
    dataset = Dataset.model_validate(raw)
    result = liquidity(dataset, dataset.scope)
    assert result.average_net_burn == Decimal("3.33")  # Empty months remain in denominator.
    assert result.runway_status == "cash_depleted" and result.runway_months == Decimal("0.00")
    assert result.last_month_stress_runway_months is None
    assert result.gross_coverage_months == Decimal("0.00")
    assert result.cash_gap_intervals[0].start == date(2026, 2, 10)
    signals, warnings = anomalies(dataset, dataset.scope)
    assert len(signals.findings) == 1
    assert signals.findings[0].kind == "new_spend"
    assert signals.findings[0].threshold_amount is None
    assert any(w.startswith("INSUFFICIENT_BASELINE") for w in warnings)


def test_debits_partition_and_tax_priority(golden):
    raw = golden["harbour-trade"].model_dump(mode="json")
    original = next(t for t in raw["transactions"] if t["category"] == "rent")
    rows = []
    cases = [
        ("rent", "operating", "potentially_deductible"),
        ("unknown", "operating", "review_required"),
        ("inventory", "operating", "review_required"),
        ("capital_equipment", "investing", "review_required"),
        ("unknown", "financing", "outside_expense_scope"),
        ("taxes_other", "tax", "review_required"),
        ("rd_contractors", "operating", "review_required"),
    ]
    for index, (category, movement, _) in enumerate(cases):
        row = {
            **original,
            "transaction_id": f"test:{index}",
            "category": category,
            "movement_type": movement,
            "amount": "10.00",
        }
        rows.append(row)
    raw["transactions"] = rows
    dataset = Dataset.model_validate(raw)
    buckets = categorize(dataset, dataset.scope)
    assert buckets.known_opex_total == Decimal("20.00")
    assert buckets.unclassified_operating_debits_total == Decimal("10.00")
    assert buckets.inventory_total == Decimal("10.00")
    assert buckets.non_opex_total == Decimal("30.00")
    assert buckets.total_debits == Decimal("70.00")
    taxes = draft_tax(dataset, dataset.scope)
    by_id = {r.transaction_id: r for r in taxes.expense_rows}
    for index, (_, _, decision) in enumerate(cases):
        assert by_id[f"test:{index}"].decision == decision


def test_duplicate_call_ids_do_not_execute_again(golden):
    dataset = golden["cloudscale-ai"]
    turn = Turn(
        calls=(
            Call("same", TOOLS[0], dataset.scope.model_dump_json()),
            Call("same", TOOLS[1], dataset.scope.model_dump_json()),
        )
    )
    output = run(dataset, Scripted([turn]), Budgets(provider_turns=1))
    assert len(output.report.tool_results) == 1
    tool_messages = [m for m in output.messages if m["role"] == "tool"]
    assert json.loads(tool_messages[1]["content"])["status"] == "error"


def test_cli_partial_exit_and_artifacts(tmp_path, golden, monkeypatch):
    from fintech_copilot.cli import main
    from fintech_copilot.orchestration.providers import MockProvider, ProviderFailure

    async def failure(self, messages, tools, timeout):
        raise ProviderFailure("AUTH_ERROR")

    monkeypatch.setattr(MockProvider, "complete", failure)
    assert main(["demo", "--output", str(tmp_path)]) == 3
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert len(summary) == 3 and all(s["status"] == "partial" for s in summary)
    for item in summary:
        events = [
            json.loads(line)
            for line in (tmp_path / item["company_id"] / "trace.jsonl").read_text().splitlines()
        ]
        assert events[-2]["event"] == "report_written"
        assert events[-1]["event"] == "run_finished"
        assert [e["event_index"] for e in events] == list(range(len(events)))

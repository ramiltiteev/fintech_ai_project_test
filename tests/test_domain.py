from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from fintech_copilot.domain.finance import anomalies, categorize, liquidity
from fintech_copilot.domain.generator import generate
from fintech_copilot.domain.models import Dataset, Scope, Transaction
from fintech_copilot.domain.tax import classify, draft_tax
from fintech_copilot.tools.dispatcher import TOOLS, Dispatcher


def replace(dataset, **changes):
    raw = dataset.model_dump(mode="json")
    raw.update(changes)
    return Dataset.model_validate(raw)


@pytest.mark.parametrize("cid", ["apex-logistics", "cloudscale-ai", "harbour-trade"])
def test_golden_oracle(golden, expected, cid, validator):
    dataset = golden[cid]
    actual = liquidity(dataset, dataset.scope).model_dump(mode="json")
    categories = categorize(dataset, dataset.scope).model_dump(mode="json")
    tax = draft_tax(dataset, dataset.scope).model_dump(mode="json")
    for key, value in expected[cid].items():
        if key in actual:
            assert actual[key] == value, key
        elif key in categories:
            assert categories[key] == value, key
    assert len(dataset.transactions) == expected[cid]["transaction_count"]
    assert tax["decision_totals"] == expected[cid]["tax_decision_totals"]
    assert sum(map(Decimal, tax["decision_totals"].values())) == Decimal(tax["total_cash_debits"])
    if cid == "apex-logistics":
        assert actual["payment_lag"]["median_days"] == expected[cid]["payment_lag_median_days"]
    dispatcher = Dispatcher(dataset)
    for name in TOOLS:
        result = dispatcher.dispatch(name, dataset.scope.model_dump_json())
        assert result.status == "ok"
        validator("tool-result.schema.json").validate(result.model_dump(mode="json"))


def test_anomaly_oracle_and_causal_baseline(golden, expected):
    dataset = golden["cloudscale-ai"]
    output, warnings = anomalies(dataset, dataset.scope)
    oracle = expected["cloudscale-ai"]
    outlier = next(f for f in output.findings if f.kind == "transaction_outlier")
    assert outlier.transaction_ids == (oracle["outlier_transaction_id"],)
    assert outlier.amount == Decimal(oracle["outlier_amount"])
    assert outlier.baseline_amount == Decimal(oracle["outlier_baseline_median"])
    assert len(outlier.baseline_transaction_ids) == oracle["outlier_baseline_count"]
    assert outlier.threshold_amount == Decimal(oracle["outlier_threshold"])
    by_id = {t.transaction_id: t for t in dataset.transactions}
    assert all(
        by_id[tx_id].posted_on < outlier.observed_on.replace(day=1)
        for tx_id in outlier.baseline_transaction_ids
    )
    jump = next(f for f in output.findings if f.kind == "monthly_spend_jump")
    assert jump.amount == Decimal(oracle["monthly_jump_current"])
    assert jump.baseline_amount == Decimal(oracle["monthly_jump_previous"])
    assert any(w.startswith("INSUFFICIENT_BASELINE") for w in warnings)
    scope = Scope(**{**dataset.scope.model_dump(), "period_start": date(2026, 3, 1)})
    assert anomalies(dataset, scope)[0].findings == output.findings
    harbour = golden["harbour-trade"]
    output, _ = anomalies(harbour, harbour.scope)
    assert output.unclassified_cross_border_total == Decimal("15000.00")
    assert (
        list(output.unclassified_cross_border_transaction_ids)
        == expected["harbour-trade"]["unclassified_cross_border_ids"]
    )


@pytest.mark.parametrize("seed", [0, 42, 77])
def test_cohort_signatures(scenarios, seed, validator):
    for profile in scenarios["profiles"]:
        cid = profile["company"]["company_id"]
        dataset = generate(scenarios, cid, seed)
        assert len(dataset.transactions) == profile["transaction_count"]
        assert dataset == generate(scenarios, cid, seed)
        assert dataset.snapshot_id != generate(scenarios, cid, seed + 1).snapshot_id
        validator("dataset.schema.json").validate(dataset.model_dump(mode="json"))
        output = liquidity(dataset, dataset.scope)
        signals, _ = anomalies(dataset, dataset.scope)
        assert all(t.evidence.synthetic for t in dataset.transactions)
        for month in output.monthly:
            assert month.operating_inflows == Decimal(profile["monthly_operating_inflows_target"])
        if cid == "apex-logistics":
            assert output.cash_gap_intervals
            assert output.payment_lag.net_60_count > 0 and output.payment_lag.net_90_count > 0
            assert output.payment_lag.median_days == Decimal("75.00")
            assert {
                r.category
                for r in categorize(dataset, dataset.scope).category_totals
                if r.total > 0
            } == set(profile["opex_shares"])
        elif cid == "cloudscale-ai":
            assert output.runway_months == Decimal("2.45") and output.low_runway
            assert output.closing_cash == Decimal("270000.00")
            assert any(f.transaction_ids == (f"{cid}:abnormal_debit",) for f in signals.findings)
        else:
            assert len(signals.unclassified_cross_border_transaction_ids) >= 3
            assert draft_tax(dataset, dataset.scope).decision_totals.non_deductible == Decimal(
                "10000.00"
            )


@pytest.mark.parametrize(
    "amount", [1.2, 1, "NaN", "Infinity", "-1.00", "0.00", "1.001", "1.0", True]
)
def test_invalid_money(golden, amount):
    raw = golden["harbour-trade"].transactions[0].model_dump(mode="json")
    raw["amount"] = amount
    with pytest.raises(ValidationError):
        Transaction.model_validate(raw)


@pytest.mark.parametrize(
    "change",
    [
        {"posted_on": "2026-02-30"},
        {"currency": "USD"},
        {"category": "revenue", "direction": "debit"},
        {"invoice_issued_on": "2026-01-01", "payment_terms_days": None},
        {"invoice_issued_on": "2027-01-01", "payment_terms_days": 60},
        {"extra": "injected"},
    ],
)
def test_invalid_transaction(golden, change):
    raw = golden["harbour-trade"].transactions[0].model_dump(mode="json")
    raw.update(change)
    with pytest.raises(ValidationError):
        Transaction.model_validate(raw)


def test_snapshot_immutable_sorted_and_unique(golden):
    dataset = golden["apex-logistics"]
    with pytest.raises(ValidationError):
        dataset.opening_balance = Decimal("1.00")
    with pytest.raises(ValidationError):
        replace(dataset, transactions=[dataset.transactions[0].model_dump(mode="json")] * 2)
    with pytest.raises(ValidationError):
        replace(dataset, period_end="2026-03-01")
    reversed_dataset = replace(
        dataset, transactions=[t.model_dump(mode="json") for t in reversed(dataset.transactions)]
    )
    assert reversed_dataset == dataset


def test_empty_months_cash_status_and_null_shares(golden):
    empty = replace(golden["harbour-trade"], transactions=[])
    result = liquidity(empty, empty.scope)
    assert len(result.monthly) == 3
    assert result.runway_status == "not_burning" and result.runway_months is None
    assert result.gross_coverage_months is None
    assert all(row.share_percent is None for row in categorize(empty, empty.scope).category_totals)
    depleted = replace(empty, opening_balance="0.00")
    result = liquidity(depleted, depleted.scope)
    assert result.runway_status == "cash_depleted" and result.runway_months == Decimal("0.00")
    assert result.low_runway and result.last_month_stress_low_runway
    assert not result.cash_gap_intervals
    negative = replace(empty, opening_balance="-1.00")
    result = liquidity(negative, negative.scope)
    assert result.cash_gap_intervals[0].start == negative.period_start
    assert result.cash_gap_intervals[0].end == negative.period_end
    assert result.cash_gap_intervals[0].open_at_period_end


def test_financing_tax_subperiod_and_end_exclusive(golden):
    dataset = golden["harbour-trade"]
    raw = dataset.transactions[0].model_dump(mode="json")
    raw.update(
        transaction_id="investor",
        direction="credit",
        category="unknown",
        movement_type="financing",
        amount="9000.00",
        posted_on="2026-01-01",
    )
    updated = replace(
        dataset, transactions=[t.model_dump(mode="json") for t in dataset.transactions] + [raw]
    )
    normal = liquidity(dataset, dataset.scope)
    result = liquidity(updated, updated.scope)
    assert result.closing_cash == normal.closing_cash + Decimal("9000.00")
    assert result.average_net_burn == normal.average_net_burn
    scope = Scope(**{**dataset.scope.model_dump(), "period_start": date(2026, 2, 1)})
    prior = Scope(**{**dataset.scope.model_dump(), "period_end": date(2026, 2, 1)})
    assert liquidity(dataset, scope).opening_cash == liquidity(dataset, prior).closing_cash
    assert all(tx.posted_on < date(2026, 2, 1) for tx in dataset.select(prior))
    with pytest.raises(ValidationError):
        Scope(**{**dataset.scope.model_dump(), "period_start": date(2026, 1, 2)})


def test_eod_prevents_intraday_gap(golden):
    dataset = golden["harbour-trade"]
    debit = dataset.transactions[-1].model_dump(mode="json")
    debit.update(transaction_id="a-debit", amount="100.00", posted_on="2026-01-05")
    credit = {**debit, "transaction_id": "z-credit", "direction": "credit", "category": "revenue"}
    updated = replace(dataset, transactions=[debit, credit], opening_balance="10.00")
    result = liquidity(updated, updated.scope)
    assert result.minimum_cash == Decimal("10.00") and not result.cash_gap_intervals
    # Missing calendar days must remain part of the continuous interval.
    credit["posted_on"] = "2026-01-10"
    updated = replace(dataset, transactions=[debit, credit], opening_balance="10.00")
    result = liquidity(updated, updated.scope)
    assert result.cash_gap_intervals[0].start == date(2026, 1, 5)
    assert result.cash_gap_intervals[0].end == date(2026, 1, 10)


@pytest.mark.parametrize(
    "changes,decision,rule",
    [
        ({"documentation_available": False}, "review_required", "HK-REVIEW-01"),
        ({"documentation_available": None}, "review_required", "HK-REVIEW-01"),
        ({"capital_nature": True}, "review_required", "HK-CAPITAL-01"),
        ({"business_purpose_confirmed": False}, "non_deductible", "HK-PURPOSE-01"),
        ({"tax_kind": "profits_tax"}, "review_required", "HK-UNCLASSIFIED-01"),
    ],
)
def test_tax_rule_priority(golden, changes, decision, rule):
    original = next(t for t in golden["harbour-trade"].transactions if t.category == "rent")
    raw = original.model_dump(mode="json")
    raw["evidence"].update(changes)
    tx = Transaction.model_validate(raw)
    actual = classify(tx)
    assert actual[:2] == (decision, rule)
    assert all(source.startswith("https://www.ird.gov.hk/") for source in actual[2])


def test_rounding_does_not_change_threshold(golden):
    dataset = golden["cloudscale-ai"]
    # burn=110000; exact runway=2.9999999... rounds to 3.00 but remains low.
    updated = replace(dataset, opening_balance="659999.99")
    result = liquidity(updated, updated.scope)
    assert result.runway_months == Decimal("3.00") and result.low_runway


def test_scenario_financial_floats_rejected(scenarios):
    scenarios["profiles"][0]["monthly_operating_inflows_target"] = 1200000.0
    with pytest.raises(ValueError, match="decimal string"):
        generate(scenarios, "apex-logistics", 42)

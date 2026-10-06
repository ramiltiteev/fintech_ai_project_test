from __future__ import annotations

from datetime import date
from decimal import Decimal

from fintech_copilot.domain.finance import ids
from fintech_copilot.domain.models import Dataset, Scope, display
from fintech_copilot.domain.results import (
    AnomalyData,
    CategorizationData,
    Fact,
    LiquidityData,
    TaxData,
)

ToolData = LiquidityData | CategorizationData | AnomalyData | TaxData


def catalogue(dataset: Dataset, scope: Scope, result_id: str, data: ToolData) -> tuple[Fact, ...]:
    selected = dataset.select(scope)
    selected_ids = ids(selected)
    facts: list[Fact] = []

    def add(
        metric: str, value: object, unit: str, evidence: tuple[str, ...], derivation: str
    ) -> None:
        if isinstance(value, Decimal):
            value = display(value)
        elif isinstance(value, date):
            value = value.isoformat()
        elif type(value) is int:
            value = str(value)
        facts.append(
            Fact.model_validate(
                {
                    "fact_id": f"{result_id}:{metric}",
                    "tool_result_id": result_id,
                    "metric": metric,
                    "value": value,
                    "unit": unit,
                    "transaction_ids": evidence,
                    "derivation_code": derivation,
                }
            )
        )

    if isinstance(data, LiquidityData):
        cash_ids = ids(tuple(tx for tx in dataset.transactions if tx.posted_on < scope.period_end))
        prior_ids = ids(
            tuple(tx for tx in dataset.transactions if tx.posted_on < scope.period_start)
        )
        operating_ids = ids(tuple(tx for tx in selected if tx.movement_type == "operating"))
        add(
            "opening_cash",
            data.opening_cash,
            "HKD",
            prior_ids,
            "dataset_opening_balance_plus_prior_movements",
        )
        for metric in ("closing_cash", "minimum_cash", "minimum_cash_date"):
            add(
                metric,
                getattr(data, metric),
                "date" if metric.endswith("date") else "HKD",
                cash_ids,
                "dataset_opening_balance_plus_all_movements",
            )
        for metric in (
            "operating_inflows",
            "operating_outflows",
            "average_net_burn",
            "last_month_net_burn",
        ):
            add(metric, getattr(data, metric), "HKD", operating_ids, "monthly_operating_net_burn")
        for metric in ("runway_months", "last_month_stress_runway_months", "gross_coverage_months"):
            add(metric, getattr(data, metric), "months", cash_ids, "cash_over_operating_burn")
        add("runway_status", data.runway_status, "status", cash_ids, "cash_and_burn_status")
        add("low_runway", data.low_runway, "flag", cash_ids, "unrounded_runway_threshold")
        add(
            "last_month_stress_low_runway",
            data.last_month_stress_low_runway,
            "flag",
            cash_ids,
            "unrounded_stress_threshold",
        )
        gap_ids = tuple(f"gap:{gap.start.isoformat()}" for gap in data.cash_gap_intervals)
        add("cash_gap_ids", gap_ids, "ids", cash_ids, "negative_end_of_day_intervals")
        for gap_id, gap in zip(gap_ids, data.cash_gap_intervals, strict=True):
            add(f"{gap_id}:start", gap.start, "date", cash_ids, "negative_end_of_day_intervals")
            add(f"{gap_id}:end", gap.end, "date", cash_ids, "negative_end_of_day_intervals")
        for field in ("receipt_count", "median_days", "net_60_count", "net_90_count"):
            add(
                f"payment_lag:{field}",
                getattr(data.payment_lag, field),
                "days" if field == "median_days" else "count",
                data.payment_lag.transaction_ids,
                "paid_invoice_receipts",
            )
    elif isinstance(data, CategorizationData):
        for metric in (
            "known_opex_total",
            "unclassified_operating_debits_total",
            "inventory_total",
            "non_opex_total",
            "total_debits",
        ):
            add(metric, getattr(data, metric), "HKD", selected_ids, "exclusive_cash_debit_buckets")
        for row in data.category_totals:
            prefix = f"category:{row.category}"
            add(f"{prefix}:total", row.total, "HKD", row.transaction_ids, "known_opex_category")
            add(
                f"{prefix}:share_percent",
                row.share_percent,
                "percent",
                tuple(
                    dict.fromkeys(
                        tx_id for r in data.category_totals for tx_id in r.transaction_ids
                    )
                ),
                "category_over_known_opex",
            )
            add(
                f"{prefix}:concentration_flag",
                row.concentration_flag,
                "flag",
                row.transaction_ids,
                "unrounded_category_threshold",
            )
        add(
            "unclassified_transaction_ids",
            data.unclassified_transaction_ids,
            "ids",
            data.unclassified_transaction_ids,
            "unknown_operating_debits",
        )
    elif isinstance(data, AnomalyData):
        footprint = tuple(
            dict.fromkeys(
                selected_ids
                + tuple(tx_id for a in data.findings for tx_id in a.baseline_transaction_ids)
            )
        )
        add(
            "anomaly_ids",
            tuple(a.anomaly_id for a in data.findings),
            "ids",
            footprint,
            "historical_anomaly_signals",
        )
        for anomaly in data.findings:
            evidence = tuple(
                dict.fromkeys(anomaly.transaction_ids + anomaly.baseline_transaction_ids)
            )
            for field in ("amount", "baseline_amount", "threshold_amount"):
                add(
                    f"anomaly:{anomaly.anomaly_id}:{field}",
                    getattr(anomaly, field),
                    "HKD",
                    evidence,
                    anomaly.method,
                )
        add(
            "unclassified_cross_border_total",
            data.unclassified_cross_border_total,
            "HKD",
            data.unclassified_cross_border_transaction_ids,
            "unknown_cross_border_review",
        )
        add(
            "unclassified_cross_border_ids",
            data.unclassified_cross_border_transaction_ids,
            "ids",
            data.unclassified_cross_border_transaction_ids,
            "unknown_cross_border_review",
        )
    else:
        for decision in type(data.decision_totals).model_fields:
            rows = tuple(
                row.transaction_id for row in data.expense_rows if row.decision == decision
            )
            add(
                f"tax:{decision}:total",
                getattr(data.decision_totals, decision),
                "HKD",
                rows,
                "hk_expense_review_rules",
            )
            add(f"tax:{decision}:ids", rows, "ids", rows, "hk_expense_review_rules")
        add(
            "tax:review_rule_ids",
            tuple(
                sorted({r.rule_id for r in data.expense_rows if r.decision == "review_required"})
            ),
            "ids",
            tuple(r.transaction_id for r in data.expense_rows if r.decision == "review_required"),
            "hk_expense_review_rules",
        )
        add(
            "tax:status",
            data.status,
            "status",
            tuple(row.transaction_id for row in data.expense_rows),
            "draft_requires_human_review",
        )
        add(
            "tax:total_cash_debits",
            data.total_cash_debits,
            "HKD",
            tuple(row.transaction_id for row in data.expense_rows),
            "all_debit_reconciliation",
        )
    return tuple(facts)

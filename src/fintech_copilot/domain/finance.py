from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from statistics import median

from .models import OPEX, ZERO, Dataset, Scope, Transaction, display
from .results import (
    Anomaly,
    AnomalyData,
    CategorizationData,
    CategoryTotal,
    Gap,
    LiquidityData,
    Month,
    PaymentLag,
)


def rounded(value: Decimal) -> Decimal:
    return Decimal(display(value))


def ids(transactions: tuple[Transaction, ...]) -> tuple[str, ...]:
    return tuple(tx.transaction_id for tx in transactions)


def total(transactions: tuple[Transaction, ...]) -> Decimal:
    return sum((tx.amount for tx in transactions), ZERO)


def next_month(day: date) -> date:
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def previous_month(day: date) -> date:
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


def months(start: date, end: date) -> tuple[date, ...]:
    values = []
    while start < end:
        values.append(start)
        start = next_month(start)
    return tuple(values)


def liquidity(dataset: Dataset, scope: Scope) -> LiquidityData:
    selected = dataset.select(scope)
    opening = dataset.opening_balance + sum(
        (tx.signed for tx in dataset.transactions if tx.posted_on < scope.period_start), ZERO
    )
    cash = minimum = opening
    minimum_date = scope.period_start
    daily: dict[date, Decimal] = {}
    for tx in selected:
        daily[tx.posted_on] = daily.get(tx.posted_on, ZERO) + tx.signed
    gaps: list[Gap] = []
    gap_start = None
    day = scope.period_start
    while day < scope.period_end:
        cash += daily.get(day, ZERO)
        if cash < minimum:
            minimum, minimum_date = cash, day
        if cash < ZERO and gap_start is None:
            gap_start = day
        elif cash >= ZERO and gap_start is not None:
            gaps.append(Gap(start=gap_start, end=day, open_at_period_end=False))
            gap_start = None
        day += timedelta(days=1)
    if gap_start is not None:
        gaps.append(Gap(start=gap_start, end=scope.period_end, open_at_period_end=True))
    monthly = []
    for month in months(scope.period_start, scope.period_end):
        operating = tuple(
            tx
            for tx in selected
            if tx.movement_type == "operating" and month <= tx.posted_on < next_month(month)
        )
        incoming = total(tuple(tx for tx in operating if tx.direction == "credit"))
        outgoing = total(tuple(tx for tx in operating if tx.direction == "debit"))
        monthly.append(
            Month(
                month=month.strftime("%Y-%m"),
                operating_inflows=incoming,
                operating_outflows=outgoing,
                net_burn=outgoing - incoming,
                transaction_ids=ids(operating),
            )
        )
    inflows = sum((m.operating_inflows for m in monthly), ZERO)
    outflows = sum((m.operating_outflows for m in monthly), ZERO)
    burn = (outflows - inflows) / len(monthly)
    last_burn = monthly[-1].net_burn
    runway = max(cash, ZERO) / burn if burn > ZERO else None
    if cash <= ZERO:
        status, runway = "cash_depleted", ZERO
    elif burn <= ZERO:
        status = "not_burning"
    else:
        status = "finite"
    stress = max(cash, ZERO) / last_burn if last_burn > ZERO else None
    gross = max(cash, ZERO) / (outflows / len(monthly)) if outflows > ZERO else None
    receipts = tuple(tx for tx in selected if tx.invoice_issued_on is not None)
    lags = [
        Decimal((tx.posted_on - tx.invoice_issued_on).days)
        for tx in receipts
        if tx.invoice_issued_on is not None
    ]
    return LiquidityData.model_validate(
        {
            "opening_cash": opening,
            "closing_cash": cash,
            "minimum_cash": minimum,
            "minimum_cash_date": minimum_date,
            "cash_gap_intervals": tuple(gaps),
            "operating_inflows": inflows,
            "operating_outflows": outflows,
            "monthly": tuple(monthly),
            "average_net_burn": rounded(burn),
            "last_month_net_burn": last_burn,
            "runway_status": status,
            "runway_months": rounded(runway) if runway is not None else None,
            "last_month_stress_runway_months": rounded(stress) if stress is not None else None,
            "gross_coverage_months": rounded(gross) if gross is not None else None,
            "low_runway": cash <= ZERO or (runway is not None and runway < 3),
            "last_month_stress_low_runway": cash <= ZERO or (stress is not None and stress < 3),
            "payment_lag": PaymentLag(
                receipt_count=len(receipts),
                median_days=rounded(median(lags)) if lags else None,
                net_60_count=sum(tx.payment_terms_days == 60 for tx in receipts),
                net_90_count=sum(tx.payment_terms_days == 90 for tx in receipts),
                transaction_ids=ids(receipts),
            ),
        }
    )


def categorize(dataset: Dataset, scope: Scope) -> CategorizationData:
    debits = tuple(tx for tx in dataset.select(scope) if tx.direction == "debit")
    known = tuple(tx for tx in debits if tx.movement_type == "operating" and tx.category in OPEX)
    unknown = tuple(
        tx for tx in debits if tx.movement_type == "operating" and tx.category == "unknown"
    )
    inventory = tuple(
        tx for tx in debits if tx.movement_type == "operating" and tx.category == "inventory"
    )
    grouped = set(ids(known + unknown + inventory))
    other = tuple(tx for tx in debits if tx.transaction_id not in grouped)
    known_total = total(known)
    categories = []
    for category in sorted(OPEX):
        rows = tuple(tx for tx in known if tx.category == category)
        amount = total(rows)
        share = amount / known_total * 100 if known_total > ZERO else None
        categories.append(
            CategoryTotal.model_validate(
                {
                    "category": category,
                    "total": amount,
                    "share_percent": rounded(share) if share is not None else None,
                    "transaction_ids": ids(rows),
                    "concentration_flag": share is not None and share >= 40,
                }
            )
        )
    return CategorizationData(
        known_opex_total=known_total,
        unclassified_operating_debits_total=total(unknown),
        inventory_total=total(inventory),
        non_opex_total=total(other),
        total_debits=total(debits),
        category_totals=tuple(categories),
        unclassified_transaction_ids=ids(unknown),
        inventory_transaction_ids=ids(inventory),
        non_opex_transaction_ids=ids(other),
    )


def anomalies(dataset: Dataset, scope: Scope) -> tuple[AnomalyData, tuple[str, ...]]:
    selected = dataset.select(scope)
    config = dataset.anomaly_config
    history = tuple(
        tx
        for tx in dataset.transactions
        if tx.movement_type == "operating" and tx.direction == "debit"
    )
    candidates = tuple(
        tx for tx in selected if tx.movement_type == "operating" and tx.direction == "debit"
    )
    findings = []
    warnings = set()
    for tx in candidates:
        month = tx.posted_on.replace(day=1)
        earliest = month
        for _ in range(config.baseline_lookback_months):
            if earliest <= dataset.period_start:
                break
            earliest = previous_month(earliest)
        baseline = tuple(
            t for t in history if earliest <= t.posted_on < month and t.category == tx.category
        )
        if len(baseline) < config.minimum_baseline_count:
            warnings.add(f"INSUFFICIENT_BASELINE:{month:%Y-%m}:{tx.category}")
        else:
            center = median([t.amount for t in baseline])
            threshold = max(
                center * config.transaction_ratio, center + config.transaction_min_delta
            )
            if tx.amount >= threshold:
                findings.append(
                    Anomaly(
                        anomaly_id=f"outlier:{tx.transaction_id}",
                        kind="transaction_outlier",
                        amount=tx.amount,
                        baseline_amount=rounded(center),
                        threshold_amount=rounded(threshold),
                        observed_on=tx.posted_on,
                        transaction_ids=(tx.transaction_id,),
                        baseline_transaction_ids=ids(baseline),
                        method="historical_median_ratio",
                        caveats=("Signal does not establish cause",),
                    )
                )
        if tx.category == "unknown" and tx.counterparty_country != "HK":
            findings.append(
                Anomaly(
                    anomaly_id=f"cross-border:{tx.transaction_id}",
                    kind="unclassified_cross_border",
                    amount=tx.amount,
                    baseline_amount=None,
                    threshold_amount=None,
                    observed_on=tx.posted_on,
                    transaction_ids=(tx.transaction_id,),
                    baseline_transaction_ids=(),
                    method="unknown_cross_border_review",
                    caveats=("Country does not establish fraud or tax treatment",),
                )
            )
    for month in months(scope.period_start, scope.period_end):
        previous = previous_month(month)
        if previous < dataset.period_start:
            warnings.add(f"NO_PREVIOUS_MONTH:{month:%Y-%m}")
            continue
        current_rows = tuple(t for t in history if month <= t.posted_on < next_month(month))
        previous_rows = tuple(t for t in history if previous <= t.posted_on < month)
        current, prior = total(current_rows), total(previous_rows)
        threshold = max(prior * config.monthly_jump_ratio, prior + config.monthly_jump_min_delta)
        if (prior > ZERO and current >= threshold) or (prior == ZERO and current > ZERO):
            findings.append(
                Anomaly(
                    anomaly_id=f"monthly:{month:%Y-%m}",
                    kind="new_spend" if prior == ZERO else "monthly_spend_jump",
                    amount=current,
                    baseline_amount=prior,
                    threshold_amount=rounded(threshold) if prior > ZERO else None,
                    observed_on=month,
                    transaction_ids=ids(current_rows),
                    baseline_transaction_ids=ids(previous_rows),
                    method="previous_month_comparison",
                    caveats=("Retrospective comparison, not a forecast",),
                )
            )
    dedup = {(f.kind, tuple(sorted(f.transaction_ids))): f for f in findings}
    unknown = tuple(
        tx for tx in candidates if tx.category == "unknown" and tx.counterparty_country != "HK"
    )
    return AnomalyData(
        findings=tuple(sorted(dedup.values(), key=lambda f: (f.kind, f.observed_on, f.anomaly_id))),
        unclassified_cross_border_total=total(unknown),
        unclassified_cross_border_transaction_ids=ids(unknown),
    ), tuple(sorted(warnings))

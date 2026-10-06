from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal
from random import Random
from typing import Any

from .finance import months
from .models import Dataset, display, parse_money


def split_cents(amount: Decimal, count: int, rng: Random) -> list[str]:
    """Allocate an exact target using integer cents and seeded positive weights."""
    cents = int(amount * 100)
    weights = [rng.randint(80, 120) for _ in range(count)]
    denominator = sum(weights)
    allocations = [cents * weight // denominator for weight in weights]
    remainder = cents - sum(allocations)
    for index in range(remainder):
        allocations[index % count] += 1
    return [display(Decimal(value) / 100) for value in allocations]


def generate(config: dict[str, Any], company_id: str, seed: int) -> Dataset:
    if type(seed) is not int or seed < 0:
        raise ValueError("Seed must be a nonnegative integer")
    profile = next(
        (p for p in config["profiles"] if p["company"]["company_id"] == company_id), None
    )
    if profile is None:
        raise ValueError("Unknown company")
    start = date.fromisoformat(config["period_start"])
    end = date.fromisoformat(config["period_end"])
    rng = Random(f"{seed}:{company_id}")
    rows: list[dict[str, Any]] = []

    def append(
        category: str,
        amount: str,
        day: date,
        direction: str = "debit",
        movement: str = "operating",
        injected_id: str | None = None,
    ) -> None:
        index = len(rows) + 1
        country = rng.choice(["CN", "SG", "US"]) if category == "unknown" else "HK"
        terms = (
            (60 if index % 2 == 0 else 90)
            if company_id == "apex-logistics" and direction == "credit"
            else None
        )
        rows.append(
            {
                "transaction_id": injected_id or f"{company_id}:{index:04d}",
                "posted_on": day.isoformat(),
                "amount": amount,
                "currency": "HKD",
                "direction": direction,
                "movement_type": movement,
                "category": category,
                "counterparty": f"Synthetic {category} counterparty {index % 7}",
                "counterparty_country": country,
                "description": f"Synthetic {category} movement",
                "invoice_issued_on": (day - timedelta(days=terms)).isoformat() if terms else None,
                "payment_terms_days": terms,
                "evidence": {
                    "synthetic": True,
                    "business_purpose_confirmed": True,
                    "chargeable_profits_link_confirmed": True,
                    "documentation_available": category != "unknown",
                    "capital_nature": False,
                    "tax_kind": "profits_tax" if category == "profits_tax" else None,
                },
            }
        )

    shares = profile.get("opex_shares", profile.get("cash_category_shares"))
    inflow = parse_money(profile["monthly_operating_inflows_target"])
    outflow = parse_money(
        profile.get(
            "monthly_operating_outflows_target",
            profile.get("monthly_base_operating_outflows_target"),
        )
    )
    injection_count = int("abnormal_debit" in profile["injections"]) + int(
        "profits_tax_payment" in profile["injections"]
    )
    baseline_count = profile["transaction_count"] - injection_count
    monthly_dates = months(start, end)
    for month_index, month in enumerate(monthly_dates):
        count = baseline_count // len(monthly_dates) + int(
            month_index < baseline_count % len(monthly_dates)
        )
        receipt_count = 6
        debit_count = count - receipt_count
        counts = {category: 1 for category in shares}
        remaining = debit_count - len(shares)
        share_weights = {category: parse_money(value) for category, value in shares.items()}
        allocated = 0
        for category, weight in share_weights.items():
            extra = int(Decimal(remaining) * weight)
            counts[category] += extra
            allocated += extra
        for index in range(remaining - allocated):
            counts[list(shares)[index % len(shares)]] += 1
        for category, weight in share_weights.items():
            for amount in split_cents(outflow * weight, counts[category], rng):
                day = rng.randint(2, 18) if company_id == "apex-logistics" else rng.randint(1, 27)
                append(category, amount, month.replace(day=day))
        for amount in split_cents(inflow, receipt_count, rng):
            day = rng.randint(26, 28) if company_id == "apex-logistics" else rng.randint(1, 27)
            append("revenue", amount, month.replace(day=day), direction="credit")
    for key, category in (
        ("abnormal_debit", "rd_contractors"),
        ("profits_tax_payment", "profits_tax"),
    ):
        injection = profile["injections"].get(key)
        if injection:
            append(
                category,
                injection["amount"],
                date.fromisoformat(injection["posted_on"]),
                movement="tax" if category == "profits_tax" else "operating",
                injected_id=f"{company_id}:{key}",
            )
    if len(rows) != profile["transaction_count"]:
        raise ValueError("Generated count differs from scenario")
    payload = {
        "schema_version": "1.0",
        "snapshot_id": "pending",
        "company": profile["company"],
        "seed": seed,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "opening_balance": profile["opening_balance"],
        "transactions": rows,
    }
    dataset = Dataset.model_validate(payload)
    canonical = dataset.model_dump(mode="json", exclude={"snapshot_id"})
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()[:24]
    payload = dataset.model_dump(mode="json")
    payload["snapshot_id"] = f"snapshot:{digest}"
    return Dataset.model_validate(payload)

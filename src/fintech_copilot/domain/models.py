from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StrictBool,
    WithJsonSchema,
    model_validator,
)

MONEY_PATTERN = r"^-?(0|[1-9][0-9]*)\.[0-9]{2}$"
ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def display(value: Decimal) -> str:
    return format(value.quantize(CENT, rounding=ROUND_HALF_UP), ".2f")


def parse_money(value: object) -> Decimal:
    if isinstance(value, Decimal):
        if not value.is_finite() or value.as_tuple().exponent != -2:
            raise ValueError("Money requires a finite Decimal with two decimal places")
        return value
    if not isinstance(value, str) or not re.fullmatch(MONEY_PATTERN, value):
        raise ValueError("Money must be a decimal string with two decimal places")
    return Decimal(value)


def parse_date(value: object) -> date:
    if type(value) is date:
        return value
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Date must be YYYY-MM-DD")
    return date.fromisoformat(value)


Money = Annotated[
    Decimal,
    BeforeValidator(parse_money),
    PlainSerializer(display, return_type=str),
    WithJsonSchema({"type": "string", "pattern": MONEY_PATTERN}),
]
Day = Annotated[date, BeforeValidator(parse_date)]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]
Category = Literal[
    "revenue",
    "rent",
    "fuel",
    "logistics",
    "subcontractors",
    "rd_contractors",
    "cloud_services",
    "other_opex",
    "inventory",
    "capital_equipment",
    "profits_tax",
    "taxes_other",
    "unknown",
]
OPEX = frozenset(
    {
        "rent",
        "fuel",
        "logistics",
        "subcontractors",
        "rd_contractors",
        "cloud_services",
        "other_opex",
    }
)


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class Company(FrozenModel):
    company_id: Identifier
    name: Annotated[str, Field(min_length=1)]
    industry: Annotated[str, Field(min_length=1)]
    jurisdiction: Literal["HK"]
    functional_currency: Literal["HKD"]


class Evidence(FrozenModel):
    synthetic: Literal[True]
    business_purpose_confirmed: StrictBool | None
    chargeable_profits_link_confirmed: StrictBool | None
    documentation_available: StrictBool | None
    capital_nature: StrictBool | None
    tax_kind: Literal["profits_tax", "other", "unknown"] | None


class Transaction(FrozenModel):
    transaction_id: Identifier
    posted_on: Day
    amount: Money
    currency: Literal["HKD"]
    direction: Literal["credit", "debit"]
    movement_type: Literal["operating", "financing", "investing", "tax", "transfer"]
    category: Category
    counterparty: Annotated[str, Field(min_length=1)]
    counterparty_country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
    description: Annotated[str, Field(min_length=1)]
    invoice_issued_on: Day | None
    payment_terms_days: Literal[60, 90] | None
    evidence: Evidence

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.amount <= ZERO:
            raise ValueError("Transaction amount must be positive")
        if (self.direction == "debit" and self.category == "revenue") or (
            self.direction == "credit" and self.category not in {"revenue", "unknown"}
        ):
            raise ValueError("Category and direction mismatch")
        if (self.invoice_issued_on is None) != (self.payment_terms_days is None):
            raise ValueError("Invoice date and terms must both be supplied")
        if self.invoice_issued_on is not None and (
            self.direction != "credit"
            or self.movement_type != "operating"
            or self.invoice_issued_on > self.posted_on
        ):
            raise ValueError("Invoice metadata requires an operating receipt after issue")
        return self

    @property
    def signed(self) -> Decimal:
        return self.amount if self.direction == "credit" else -self.amount


def validate_period(start: date, end: date) -> None:
    if start >= end or start.day != 1 or end.day != 1:
        raise ValueError("Period must contain full calendar months [start, end)")


class Scope(FrozenModel):
    company_id: Identifier
    snapshot_id: Identifier
    period_start: Day
    period_end: Day

    @model_validator(mode="after")
    def valid_period(self) -> Self:
        validate_period(self.period_start, self.period_end)
        return self


class AnomalyConfig(FrozenModel):
    """Server-owned detection rules, persisted with the financial snapshot."""

    minimum_baseline_count: Annotated[int, Field(strict=True, ge=1)] = 5
    baseline_lookback_months: Annotated[int, Field(strict=True, ge=1)] = 3
    transaction_ratio: Annotated[Money, Field(ge=Decimal("1.00"))] = Decimal("3.00")
    transaction_min_delta: Annotated[Money, Field(ge=ZERO)] = Decimal("10000.00")
    monthly_jump_ratio: Annotated[Money, Field(ge=Decimal("1.00"))] = Decimal("1.50")
    monthly_jump_min_delta: Annotated[Money, Field(ge=ZERO)] = Decimal("10000.00")


class Dataset(FrozenModel):
    schema_version: Literal["1.0"]
    snapshot_id: Identifier
    company: Company
    seed: Annotated[int, Field(strict=True, ge=0)]
    period_start: Day
    period_end: Day
    opening_balance: Money
    transactions: tuple[Transaction, ...]
    anomaly_config: AnomalyConfig = Field(default_factory=AnomalyConfig)

    @model_validator(mode="after")
    def valid_snapshot(self) -> Self:
        validate_period(self.period_start, self.period_end)
        ids = [tx.transaction_id for tx in self.transactions]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate transaction ID")
        if any(not self.period_start <= tx.posted_on < self.period_end for tx in self.transactions):
            raise ValueError("Transaction outside snapshot coverage")
        object.__setattr__(
            self,
            "transactions",
            tuple(sorted(self.transactions, key=lambda tx: (tx.posted_on, tx.transaction_id))),
        )
        return self

    @property
    def scope(self) -> Scope:
        return Scope(
            company_id=self.company.company_id,
            snapshot_id=self.snapshot_id,
            period_start=self.period_start,
            period_end=self.period_end,
        )

    def select(self, scope: Scope) -> tuple[Transaction, ...]:
        if scope.company_id != self.company.company_id:
            raise ValueError("COMPANY_NOT_FOUND")
        if scope.snapshot_id != self.snapshot_id:
            raise ValueError("SNAPSHOT_MISMATCH")
        if not self.period_start <= scope.period_start < scope.period_end <= self.period_end:
            raise ValueError("INVALID_PERIOD")
        return tuple(
            tx for tx in self.transactions if scope.period_start <= tx.posted_on < scope.period_end
        )

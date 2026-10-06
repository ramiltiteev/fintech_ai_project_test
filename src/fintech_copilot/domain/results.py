from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, model_validator

from .models import Category, Day, FrozenModel, Identifier, Money

Ids = tuple[Identifier, ...]
ToolName = Literal[
    "CalculateLiquidityRunway", "CategorizeOpex", "DetectCashFlowAnomalies", "DraftHKProfitsTax"
]
Decision = Literal[
    "potentially_deductible", "non_deductible", "review_required", "outside_expense_scope"
]


class Gap(FrozenModel):
    start: Day
    end: Day
    open_at_period_end: StrictBool


class Month(FrozenModel):
    month: Annotated[str, Field(pattern=r"^\d{4}-\d{2}$")]
    operating_inflows: Money
    operating_outflows: Money
    net_burn: Money
    transaction_ids: Ids


class PaymentLag(FrozenModel):
    receipt_count: Annotated[int, Field(strict=True, ge=0)]
    median_days: Money | None
    net_60_count: Annotated[int, Field(strict=True, ge=0)]
    net_90_count: Annotated[int, Field(strict=True, ge=0)]
    transaction_ids: Ids


class LiquidityData(FrozenModel):
    opening_cash: Money
    closing_cash: Money
    minimum_cash: Money
    minimum_cash_date: Day
    cash_gap_intervals: tuple[Gap, ...]
    operating_inflows: Money
    operating_outflows: Money
    monthly: Annotated[tuple[Month, ...], Field(min_length=1)]
    average_net_burn: Money
    last_month_net_burn: Money
    runway_status: Literal["finite", "cash_depleted", "not_burning"]
    runway_months: Money | None
    last_month_stress_runway_months: Money | None
    gross_coverage_months: Money | None
    payment_lag: PaymentLag
    low_runway: StrictBool
    last_month_stress_low_runway: StrictBool


class CategoryTotal(FrozenModel):
    category: Category
    total: Money
    share_percent: Money | None
    transaction_ids: Ids
    concentration_flag: StrictBool


class CategorizationData(FrozenModel):
    known_opex_total: Money
    unclassified_operating_debits_total: Money
    inventory_total: Money
    non_opex_total: Money
    total_debits: Money
    category_totals: tuple[CategoryTotal, ...]
    unclassified_transaction_ids: Ids
    inventory_transaction_ids: Ids
    non_opex_transaction_ids: Ids


class Anomaly(FrozenModel):
    anomaly_id: Identifier
    kind: Literal[
        "transaction_outlier", "monthly_spend_jump", "new_spend", "unclassified_cross_border"
    ]
    amount: Money
    baseline_amount: Money | None
    threshold_amount: Money | None
    observed_on: Day
    transaction_ids: Ids
    baseline_transaction_ids: Ids
    method: Identifier
    caveats: tuple[str, ...]


class AnomalyData(FrozenModel):
    findings: tuple[Anomaly, ...]
    unclassified_cross_border_total: Money
    unclassified_cross_border_transaction_ids: Ids


class ExpenseRow(FrozenModel):
    transaction_id: Identifier
    amount: Money
    category: Category
    decision: Decision
    rule_id: Identifier
    reason_code: Identifier
    source_urls: Annotated[tuple[str, ...], Field(min_length=1)]
    missing_evidence: tuple[str, ...]


class DecisionTotals(FrozenModel):
    potentially_deductible: Money
    non_deductible: Money
    review_required: Money
    outside_expense_scope: Money


class TaxData(FrozenModel):
    ruleset_id: Literal["hk-expense-review-v1"]
    rules_verified_on: Day
    status: Literal["review_required"]
    filing_ready: Literal[False]
    assessable_profit: None
    tax_payable: None
    expense_rows: tuple[ExpenseRow, ...]
    decision_totals: DecisionTotals
    total_cash_debits: Money
    missing_inputs: Annotated[tuple[str, ...], Field(min_length=1)]


class Fact(FrozenModel):
    fact_id: Identifier
    tool_result_id: Identifier
    metric: Identifier
    value: str | Ids | StrictBool | None
    unit: Literal["HKD", "months", "percent", "days", "date", "ids", "status", "count", "flag"]
    transaction_ids: Ids
    derivation_code: Identifier

    @model_validator(mode="after")
    def value_matches_unit(self) -> Self:
        from .models import parse_date, parse_money

        if self.value is None:
            if self.unit not in {"HKD", "months", "percent", "days"}:
                raise ValueError("Unexpected null fact")
        elif self.unit in {"HKD", "months", "percent", "days"}:
            parse_money(self.value)
        elif self.unit == "flag" and type(self.value) is not bool:
            raise ValueError("Flag fact must be boolean")
        elif self.unit == "ids" and not isinstance(self.value, tuple):
            raise ValueError("IDs fact must be an array")
        elif self.unit == "date":
            parse_date(self.value)
        elif self.unit == "status" and self.value not in {
            "finite",
            "cash_depleted",
            "not_burning",
            "review_required",
        }:
            raise ValueError("Unknown status fact")
        elif self.unit == "count" and (not isinstance(self.value, str) or not self.value.isdigit()):
            raise ValueError("Count fact must be a nonnegative integer string")
        if len(set(self.transaction_ids)) != len(self.transaction_ids):
            raise ValueError("Duplicate fact evidence")
        return self


class ResultBase(FrozenModel):
    schema_version: Literal["1.0"]
    tool_result_id: Identifier
    company_id: Identifier
    snapshot_id: Identifier
    period_start: Day
    period_end: Day


class SuccessBase(ResultBase):
    status: Literal["ok"]
    facts: Annotated[tuple[Fact, ...], Field(min_length=1)]
    warnings: tuple[str, ...]

    @model_validator(mode="after")
    def unique_facts(self) -> Self:
        ids = [fact.fact_id for fact in self.facts]
        if len(ids) != len(set(ids)) or any(
            fact.tool_result_id != self.tool_result_id for fact in self.facts
        ):
            raise ValueError("Duplicate or mismatched fact IDs")
        return self


class LiquidityResult(SuccessBase):
    tool_name: Literal["CalculateLiquidityRunway"]
    data: LiquidityData


class CategorizationResult(SuccessBase):
    tool_name: Literal["CategorizeOpex"]
    data: CategorizationData


class AnomalyResult(SuccessBase):
    tool_name: Literal["DetectCashFlowAnomalies"]
    data: AnomalyData


class TaxResult(SuccessBase):
    tool_name: Literal["DraftHKProfitsTax"]
    data: TaxData


class ToolError(FrozenModel):
    code: Literal[
        "INVALID_ARGUMENTS",
        "COMPANY_NOT_FOUND",
        "SNAPSHOT_MISMATCH",
        "INVALID_PERIOD",
        "UNSUPPORTED_CURRENCY",
        "INSUFFICIENT_DATA",
        "INTERNAL_TOOL_ERROR",
    ]
    message: str
    retryable: StrictBool


class ErrorResult(ResultBase):
    tool_name: ToolName
    status: Literal["error"]
    error: ToolError


Success = LiquidityResult | CategorizationResult | AnomalyResult | TaxResult
ToolResult = Success | ErrorResult

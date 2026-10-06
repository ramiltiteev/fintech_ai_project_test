from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from fintech_copilot.domain.models import Company, Day, FrozenModel, Identifier
from fintech_copilot.domain.results import Fact, TaxData, ToolResult

Finding = Literal[
    "low_runway",
    "cash_gap",
    "opex_concentration",
    "cashflow_anomaly",
    "unclassified_cross_border",
    "tax_review_required",
    "no_material_findings",
]
Action = Literal[
    "prepare_liquidity_buffer",
    "review_operating_spend",
    "review_collection_terms",
    "verify_payment_documents",
    "review_tax_treatment",
    "monitor_cash",
]


class Reference(FrozenModel):
    tool_result_id: Identifier
    fact_id: Identifier


class Insight(FrozenModel):
    finding_code: Finding
    action_code: Action
    evidence: Annotated[tuple[Reference, ...], Field(min_length=1)]


class Synthesis(FrozenModel):
    insights: Annotated[tuple[Insight, ...], Field(min_length=1, max_length=6)]

    @model_validator(mode="after")
    def no_duplicates(self) -> Self:
        codes = [insight.finding_code for insight in self.insights]
        if len(codes) != len(set(codes)):
            raise ValueError("Duplicate findings")
        return self


class Report(FrozenModel):
    schema_version: Literal["1.0"]
    run_id: Identifier
    generated_at: datetime
    company: Company
    company_id: Identifier
    snapshot_id: Identifier
    period_start: Day
    period_end: Day
    seed: Annotated[int, Field(strict=True, ge=0)]
    provider_mode: Literal["mock", "live"]
    model: str | None
    status: Literal["complete", "partial"]
    partial_reasons: tuple[str, ...]
    facts: tuple[Fact, ...]
    synthesis: Synthesis | None
    tax_draft: TaxData | None
    tool_results: tuple[ToolResult, ...]
    limitations: Annotated[tuple[str, ...], Field(min_length=1)]

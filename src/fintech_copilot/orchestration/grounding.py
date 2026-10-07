from __future__ import annotations

from fintech_copilot.domain.models import Dataset, Scope
from fintech_copilot.domain.results import (
    AnomalyResult,
    CategorizationResult,
    LiquidityResult,
    Success,
    TaxResult,
    ToolResult,
)
from fintech_copilot.tools.catalogue import catalogue
from fintech_copilot.tools.dispatcher import TOOLS

from .models import Action, Finding, Insight, Reference, Synthesis

ACTIONS: dict[Finding, tuple[Action, ...]] = {
    "low_runway": ("prepare_liquidity_buffer", "review_operating_spend"),
    "stress_low_runway": ("prepare_liquidity_buffer", "review_operating_spend"),
    "cash_gap": ("review_collection_terms", "prepare_liquidity_buffer"),
    "opex_concentration": ("review_operating_spend",),
    "cashflow_anomaly": ("verify_payment_documents",),
    "unclassified_cross_border": ("verify_payment_documents",),
    "tax_review_required": ("review_tax_treatment",),
    "no_material_findings": ("monitor_cash",),
}


class GroundingError(ValueError):
    """Server-owned validation code and location; never includes rejected input."""

    def __init__(
        self,
        code: str,
        path: tuple[str | int, ...] = (),
        issue_code: str | None = None,
        *,
        allowed_actions: tuple[Action, ...] = (),
        expected_evidence: tuple[tuple[Reference, ...], ...] = (),
    ) -> None:
        super().__init__(code)
        self.code = code
        self.path = path
        self.issue_code = issue_code or code
        self.allowed_actions = allowed_actions
        self.expected_evidence = expected_evidence


class GroundingErrors(ValueError):
    """Independent insight failures collected for a single repair attempt."""

    def __init__(self, errors: tuple[GroundingError, ...]) -> None:
        super().__init__("; ".join(error.code for error in errors))
        self.errors = errors


def full_results(results: tuple[ToolResult, ...], scope: Scope) -> tuple[Success, ...]:
    by_name: dict[str, Success] = {}
    for result in results:
        if result.status == "ok" and (
            result.company_id,
            result.snapshot_id,
            result.period_start,
            result.period_end,
        ) == (scope.company_id, scope.snapshot_id, scope.period_start, scope.period_end):
            by_name[result.tool_name] = result
    return tuple(by_name[name] for name in TOOLS if name in by_name)


def options(results: tuple[Success, ...]) -> dict[Finding, list[tuple[Reference, ...]]]:
    """Select evidence from tool-owned predicates; never calculate financial values."""
    choices: dict[Finding, list[tuple[Reference, ...]]] = {}

    def add(code: Finding, result: Success, metrics: tuple[str, ...]) -> None:
        by_metric = {fact.metric: fact for fact in result.facts}
        if any(metric not in by_metric for metric in metrics):
            return
        refs = tuple(
            Reference(tool_result_id=result.tool_result_id, fact_id=by_metric[m].fact_id)
            for m in metrics
        )
        choices.setdefault(code, []).append(refs)

    for result in results:
        if isinstance(result, LiquidityResult):
            if result.data.low_runway:
                add("low_runway", result, ("runway_months", "closing_cash", "average_net_burn"))
            if result.data.last_month_stress_low_runway:
                add(
                    "stress_low_runway",
                    result,
                    (
                        "last_month_stress_runway_months",
                        "closing_cash",
                        "last_month_net_burn",
                        "last_month_stress_low_runway",
                    ),
                )
            if result.data.cash_gap_intervals:
                add("cash_gap", result, ("cash_gap_ids", "minimum_cash"))
        elif isinstance(result, CategorizationResult):
            for row in sorted(result.data.category_totals, key=lambda r: r.total, reverse=True):
                if row.concentration_flag:
                    add(
                        "opex_concentration",
                        result,
                        (
                            f"category:{row.category}:total",
                            f"category:{row.category}:share_percent",
                        ),
                    )
        elif isinstance(result, AnomalyResult):
            for anomaly in result.data.findings:
                if anomaly.kind != "unclassified_cross_border":
                    prefix = f"anomaly:{anomaly.anomaly_id}"
                    add(
                        "cashflow_anomaly",
                        result,
                        (
                            "anomaly_ids",
                            f"{prefix}:amount",
                            f"{prefix}:baseline_amount",
                            f"{prefix}:threshold_amount",
                        ),
                    )
            if result.data.unclassified_cross_border_transaction_ids:
                add(
                    "unclassified_cross_border",
                    result,
                    ("unclassified_cross_border_ids", "unclassified_cross_border_total"),
                )
        elif isinstance(result, TaxResult) and any(
            row.decision == "review_required" for row in result.data.expense_rows
        ):
            add(
                "tax_review_required",
                result,
                ("tax:review_required:ids", "tax:review_required:total", "tax:review_rule_ids"),
            )
    if not choices and len({result.tool_name for result in results}) == len(TOOLS):
        refs = tuple(
            Reference(tool_result_id=r.tool_result_id, fact_id=r.facts[0].fact_id) for r in results
        )
        choices["no_material_findings"] = [refs]
    return choices


def mock_synthesis(results: tuple[Success, ...]) -> Synthesis:
    choices = options(results)
    return Synthesis(
        insights=tuple(
            Insight(finding_code=code, action_code=ACTIONS[code][0], evidence=evidence[0])
            for code, evidence in choices.items()
        )[:6]
    )


def validate_synthesis(
    synthesis: Synthesis, results: tuple[ToolResult, ...], dataset: Dataset
) -> None:
    successful = full_results(results, dataset.scope)
    if {r.tool_name for r in successful} != set(TOOLS):
        raise GroundingError("MISSING_FULL_PERIOD_TOOLS")
    transaction_ids = {tx.transaction_id for tx in dataset.transactions}
    facts = {}
    for result in successful:
        expected = catalogue(dataset, dataset.scope, result.tool_result_id, result.data)
        if expected != result.facts:
            raise GroundingError("FACT_DATA_MISMATCH")
        for fact in result.facts:
            if not set(fact.transaction_ids) <= transaction_ids:
                raise GroundingError("UNKNOWN_TRANSACTION_EVIDENCE")
            facts[(result.tool_result_id, fact.fact_id)] = fact
    choices = options(successful)
    errors: list[GroundingError] = []
    for index, insight in enumerate(synthesis.insights):
        path: tuple[str | int, ...] = ("insights", index)
        candidates = choices.get(insight.finding_code, [])
        expected_evidence = tuple(candidates[:3])
        if insight.action_code not in ACTIONS[insight.finding_code]:
            errors.append(
                GroundingError(
                    "INCOMPATIBLE_ACTION",
                    (*path, "action_code"),
                    allowed_actions=ACTIONS[insight.finding_code],
                )
            )
        refs: set[tuple[str, str]] = set()
        reference_error = False
        for ref_index, ref in enumerate(insight.evidence):
            pair = (ref.tool_result_id, ref.fact_id)
            ref_path = (*path, "evidence", ref_index)
            if pair in refs:
                reference_error = True
                errors.append(
                    GroundingError(
                        "FABRICATED_STALE_OR_DUPLICATE_REFERENCE",
                        ref_path,
                        "DUPLICATE_REFERENCE",
                        expected_evidence=expected_evidence,
                    )
                )
            if pair not in facts:
                reference_error = True
                errors.append(
                    GroundingError(
                        "FABRICATED_STALE_OR_DUPLICATE_REFERENCE",
                        ref_path,
                        "UNKNOWN_FACT_REFERENCE",
                        expected_evidence=expected_evidence,
                    )
                )
            refs.add(pair)
        if not candidates:
            errors.append(
                GroundingError(
                    "UNSUPPORTED_FINDING_OR_EVIDENCE",
                    (*path, "finding_code"),
                    "UNSUPPORTED_FINDING",
                )
            )
        elif not reference_error and not any(
            {(ref.tool_result_id, ref.fact_id) for ref in candidate} <= refs
            for candidate in candidates
        ):
            errors.append(
                GroundingError(
                    "UNSUPPORTED_FINDING_OR_EVIDENCE",
                    (*path, "evidence"),
                    "INSUFFICIENT_EVIDENCE",
                    expected_evidence=expected_evidence,
                )
            )
    if "stress_low_runway" in choices and not any(
        insight.finding_code == "stress_low_runway" for insight in synthesis.insights
    ):
        errors.append(
            GroundingError(
                "MISSING_STRESS_RUNWAY_FINDING",
                ("insights",),
                allowed_actions=ACTIONS["stress_low_runway"],
                expected_evidence=tuple(choices["stress_low_runway"]),
            )
        )
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise GroundingErrors(tuple(errors))

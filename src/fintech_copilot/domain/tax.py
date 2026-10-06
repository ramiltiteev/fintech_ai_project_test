from __future__ import annotations

from datetime import date
from typing import Literal

from .finance import total
from .models import OPEX, ZERO, Dataset, Scope, Transaction
from .results import Decision, DecisionTotals, ExpenseRow, TaxData

RULESET_ID: Literal["hk-expense-review-v1"] = "hk-expense-review-v1"
RULES_VERIFIED_ON = date(2026, 10, 6)
IRD_PROFITS = "https://www.ird.gov.hk/eng/tax/bus_pft.htm"
IRD_BIR51 = "https://www.ird.gov.hk/eng/tax/bir51e_notes.htm"
IRD_RD = "https://www.ird.gov.hk/eng/pdf/dipn55.pdf"
MISSING_INPUTS = (
    "Full basis period",
    "Financial statements",
    "Cash-to-accrual reconciliation",
    "Source-of-profits analysis",
    "Allowances and special deductions",
    "Human tax review",
)


def classify(tx: Transaction) -> tuple[Decision, str, tuple[str, ...]]:
    evidence = tx.evidence
    if tx.movement_type in {"financing", "transfer"}:
        return "outside_expense_scope", "HK-OUTSIDE-01", (IRD_PROFITS,)
    if evidence.tax_kind == "profits_tax" and tx.category == "profits_tax":
        return "non_deductible", "HK-TAX-01", (IRD_PROFITS,)
    if (
        evidence.capital_nature is True
        or tx.category == "capital_equipment"
        or tx.movement_type == "investing"
    ):
        return "review_required", "HK-CAPITAL-01", (IRD_PROFITS,)
    if (
        evidence.business_purpose_confirmed is False
        or evidence.chargeable_profits_link_confirmed is False
    ):
        return "non_deductible", "HK-PURPOSE-01", (IRD_PROFITS,)
    tax_inconsistent = (
        (tx.category == "profits_tax" and evidence.tax_kind != "profits_tax")
        or (tx.category != "profits_tax" and evidence.tax_kind == "profits_tax")
        or (evidence.tax_kind == "other" and tx.category != "taxes_other")
        or (tx.movement_type == "tax" and tx.category not in {"profits_tax", "taxes_other"})
    )
    if (
        tx.category in {"unknown", "taxes_other"}
        or evidence.tax_kind == "unknown"
        or tax_inconsistent
    ):
        return "review_required", "HK-UNCLASSIFIED-01", (IRD_PROFITS,)
    if tx.category in {"rd_contractors", "inventory"}:
        return (
            "review_required",
            "HK-SPECIAL-01",
            (IRD_RD if tx.category == "rd_contractors" else IRD_BIR51,),
        )
    if (
        tx.movement_type == "operating"
        and tx.category in OPEX
        and evidence.business_purpose_confirmed is True
        and evidence.chargeable_profits_link_confirmed is True
        and evidence.documentation_available is True
        and evidence.capital_nature is False
    ):
        return "potentially_deductible", "HK-OPEX-01", (IRD_PROFITS,)
    return "review_required", "HK-REVIEW-01", (IRD_PROFITS,)


def draft_tax(dataset: Dataset, scope: Scope) -> TaxData:
    debits = tuple(tx for tx in dataset.select(scope) if tx.direction == "debit")
    rows = []
    totals = {name: ZERO for name in DecisionTotals.model_fields}
    for tx in debits:
        decision, rule, sources = classify(tx)
        missing = [
            name
            for name in (
                "business_purpose_confirmed",
                "chargeable_profits_link_confirmed",
                "documentation_available",
                "capital_nature",
            )
            if getattr(tx.evidence, name) is None
        ]
        if tx.evidence.documentation_available is False:
            missing.append("supporting_documents")
        if decision == "review_required":
            missing.append("human_classification_review")
        rows.append(
            ExpenseRow(
                transaction_id=tx.transaction_id,
                amount=tx.amount,
                category=tx.category,
                decision=decision,
                rule_id=rule,
                reason_code=rule,
                source_urls=sources,
                missing_evidence=tuple(missing),
            )
        )
        totals[decision] += tx.amount
    return TaxData(
        ruleset_id=RULESET_ID,
        rules_verified_on=RULES_VERIFIED_ON,
        status="review_required",
        filing_ready=False,
        assessable_profit=None,
        tax_payable=None,
        expense_rows=tuple(rows),
        decision_totals=DecisionTotals.model_validate(totals),
        total_cash_debits=total(debits),
        missing_inputs=MISSING_INPUTS,
    )

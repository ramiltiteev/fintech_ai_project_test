"""Safe synthesis validation feedback shared by trace and the repair message."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from .chat_text import ChatTextError
from .grounding import GroundingError, GroundingErrors

MAX_VALIDATION_ERRORS = 8
SCHEMA_FIELDS = frozenset(
    {"insights", "finding_code", "action_code", "evidence", "tool_result_id", "fact_id"}
)
SCHEMA_HINTS = {
    "raw_quantity_in_text": (
        "Use a fact part for quantities or a context part for snapshot metadata."
    ),
    "token_in_text_part": "Use a fact part; text parts contain prose, without template tokens.",
    "unsafe_text_part": "Use readable text without terminal/control characters.",
    "json_invalid": "Return a single valid JSON object, without Markdown fences or prose.",
    "missing": "Include this required field from the synthesis schema.",
    "extra_forbidden": "Remove the extra field; use only fields from the synthesis schema.",
    "literal_error": "Use an allowed enum value from the synthesis schema.",
    "string_type": "Use a JSON string for this field.",
    "string_pattern_mismatch": "Copy an existing ID exactly; use the schema identifier format.",
    "too_short": "Include the minimum number of items required by the synthesis schema.",
    "too_long": "Respect the maximum number of items in the synthesis schema.",
    "tuple_type": "Use a JSON array for this field.",
    "model_type": "Use a JSON object with the fields required by the synthesis schema.",
    "value_error": "Check the payload invariants in the synthesis contract.",
    "duplicate_findings": "Each finding_code must appear only once in synthesis.insights.",
    "answer_requires_references": "For kind=answer, include real fact references.",
    "answer_forbids_synthesis": (
        "For kind=answer, set synthesis=null; synthesis is only for report."
    ),
    "report_requires_synthesis": "For kind=report, include a supported synthesis object.",
    "nonfinancial_forbids_evidence": (
        "For nonfinancial replies, use references=[] and synthesis=null."
    ),
    "method_required": "For kind=method, choose a method other than none.",
    "method_forbidden": "For this reply kind, set method=none.",
    "duplicate_plan_steps": "Use each tool/scope pair only once in the plan.",
    "plan_preserve_steps": "Keep the previous mode and steps unchanged; only append new steps.",
    "report_plan_requires_tools": "A report plan requires all four full-period tools.",
    "invalid_plan_call": "Use a new nonempty call ID and keep the plan within its size limit.",
    "invalid_plan_scope": "Copy the supplied company/snapshot and use a period within coverage.",
    "report_requires_insights": "For kind=report, include supported insights from report_options.",
    "reply_forbids_insights": "Only kind=report uses insights; for other kinds set insights=[].",
    "duplicate_model_findings": "Each finding_code may appear only once in insights.",
}


def invariant_diagnostic(code: str) -> dict[str, Any]:
    """Only callers with server-owned invariant codes may use this helper."""
    return {
        "stage": "schema",
        "reason_code": "SCHEMA_VALIDATION_FAILED",
        "validation_errors": [{"code": code, "path": [], "message": SCHEMA_HINTS[code]}],
        "errors_truncated": False,
    }


GROUNDING_HINTS = {
    "COMPOSITION_MISMATCH": "Compiled text and sources must match the original typed parts.",
    "UNKNOWN_FACT_ALIAS": "Copy an existing short ref from the fact catalogue, e.g. {{fact:f0}}.",
    "UNCITED_NUMBER": (
        "Fix each text_issues range [start,end) in the candidate text. "
        "Replace a quantitative value with {{fact:index}} for the correct existing reference. "
        "Remove unsupported values, dates or thresholds; use bullets instead of numbered labels. "
        "Do not simply append a citation after a raw number."
    ),
    "INVALID_FACT_PLACEHOLDER": "Copy an existing complete {{fact:fN}} token from the catalogue.",
    "UNUSED_FACT_REFERENCE": "Use every referenced fact in the response text.",
    "UNSAFE_CHAT_TEXT": "Return readable text without terminal/control characters.",
    "INCOMPLETE_PLAN_ANSWER": "Cite the result of every planned step in plan order.",
    "UNSUPPORTED_NARRATIVE": "Rewrite the text using only supported facts and requested tasks.",
    "MISSING_FULL_PERIOD_TOOLS": "Obtain successful full-period results from all four tools.",
    "FACT_DATA_MISMATCH": "Tool facts do not match tool data; synthesis cannot be accepted.",
    "UNKNOWN_TRANSACTION_EVIDENCE": "Tool evidence contains unknown transaction IDs.",
    "INCOMPATIBLE_ACTION": "Choose an action compatible with this finding.",
    "UNKNOWN_FACT_REFERENCE": (
        "Use an existing tool_result_id/fact_id pair from successful full-period tool results."
    ),
    "DUPLICATE_REFERENCE": "Remove the repeated evidence reference from this insight.",
    "UNSUPPORTED_FINDING": "Choose a finding whose predicate is true in the tool results.",
    "INSUFFICIENT_EVIDENCE": "Cite all facts required to support this finding, not only a subset.",
}


def synthesis_diagnostic(
    error: ValueError,
    *,
    schema_fields: frozenset[str] = SCHEMA_FIELDS,
    schema_label: str = "synthesis",
) -> dict[str, Any]:
    """Do not serialize exception text, ctx, input, or provider-supplied field names."""
    if isinstance(error, PydanticCustomError) and error.type in SCHEMA_HINTS:
        return invariant_diagnostic(error.type)
    if isinstance(error, ValidationError):
        errors = error.errors(include_input=False, include_context=False, include_url=False)
        json_error = any(item["type"] == "json_invalid" for item in errors)
        issues = []
        for item in errors[:MAX_VALIDATION_ERRORS]:
            kind = item["type"] if item["type"] in SCHEMA_HINTS else "schema_error"
            path = [
                part if isinstance(part, int) or part in schema_fields else "[extra]"
                for part in item["loc"]
            ]
            issues.append(
                {
                    "code": kind,
                    "path": path,
                    "message": (
                        SCHEMA_HINTS.get(kind, "Match the synthesis JSON schema.").replace(
                            "synthesis", schema_label
                        )
                        if kind
                        in {
                            "missing",
                            "extra_forbidden",
                            "literal_error",
                            "too_short",
                            "too_long",
                            "model_type",
                            "value_error",
                            "schema_error",
                        }
                        else SCHEMA_HINTS[kind]
                    ),
                }
            )
        return {
            "stage": "json" if json_error else "schema",
            "reason_code": "INVALID_JSON" if json_error else "SCHEMA_VALIDATION_FAILED",
            "validation_errors": issues,
            "errors_truncated": len(errors) > MAX_VALIDATION_ERRORS,
        }
    if isinstance(error, (GroundingError, GroundingErrors)):
        grounding_errors = error.errors if isinstance(error, GroundingErrors) else (error,)
        issues = []
        for failure in grounding_errors:
            issue: dict[str, Any] = {
                "code": failure.issue_code,
                "path": list(failure.path),
                "message": GROUNDING_HINTS[failure.issue_code],
            }
            if failure.allowed_actions:
                issue["message"] += " Allowed actions: " + ", ".join(failure.allowed_actions) + "."
            if failure.expected_evidence:
                issue["expected_evidence"] = [
                    [reference.model_dump(mode="json") for reference in option]
                    for option in failure.expected_evidence
                ]
            if isinstance(failure, ChatTextError):
                issue["text_issues"] = [asdict(item) for item in failure.text_issues]
                issue["text_issues_truncated"] = failure.text_issues_truncated
            issues.append(issue)
        return {
            "stage": "grounding",
            "reason_code": (
                grounding_errors[0].issue_code
                if len(grounding_errors) == 1
                else "MULTIPLE_GROUNDING_ERRORS"
            ),
            "validation_errors": issues,
            "errors_truncated": False,
        }
    return {
        "stage": "grounding",
        "reason_code": "GROUNDING_VALIDATION_FAILED",
        "validation_errors": [
            {"code": "GROUNDING_VALIDATION_FAILED", "path": [], "message": "Check tool evidence."}
        ],
        "errors_truncated": False,
    }

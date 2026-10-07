"""Short model-facing citations resolve to server-owned canonical references."""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Any, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator
from pydantic_core import PydanticCustomError

from fintech_copilot.domain.models import Dataset, FrozenModel
from fintech_copilot.domain.results import Fact, ToolResult

from .grounding import GroundingError
from .models import Action, Finding, Reference

Alias = Annotated[str, StringConstraints(pattern=r"^f(?:0|[1-9][0-9]*)$", max_length=12)]
TOKEN = re.compile(r"\{\{fact:(f(?:0|[1-9][0-9]*))\}\}")
MODEL_HINTS = {
    "answer_requires_references": (
        "For kind=answer, select real catalogue facts using fact parts."
    ),
    "answer_forbids_synthesis": "For kind=answer, use insights=[] and omit synthesis/references.",
    "report_requires_synthesis": "For kind=report, provide insights from report_options.",
    "nonfinancial_forbids_evidence": (
        "For nonfinancial replies, omit fact tokens and use insights=[]."
    ),
    "duplicate_findings": "Each finding_code may appear only once in insights.",
    "UNKNOWN_FACT_REFERENCE": "Select existing aliases in fact parts or complete insight bundles.",
    "UNKNOWN_FACT_ALIAS": "Use an existing catalogue alias as the ref of a fact part, e.g. f0.",
    "INVALID_FACT_PLACEHOLDER": "Use fact parts instead of writing citation templates in prose.",
    "UNCITED_NUMBER": (
        "Fix each text_issues range [start,end) in the candidate text. "
        "Return ComposedReply with separate text/fact/context parts. "
        "Replace a quantity with a fact part for the correct existing catalogue alias. "
        "Use a context part for snapshot metadata; remove unsupported values or thresholds. "
        "Do not copy the old text/references format."
    ),
}


class ModelInsight(FrozenModel):
    finding_code: Finding
    action_code: Action
    evidence: Annotated[tuple[Alias, ...], Field(min_length=1, max_length=64)]


class ModelReply(FrozenModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
    kind: Literal["help", "clarify", "unsupported", "method", "answer", "report"]
    method: Literal["none", "runway", "synthetic", "tax", "tools"] = "none"
    insights: Annotated[tuple[ModelInsight, ...], Field(max_length=6)] = ()

    @model_validator(mode="after")
    def payload(self) -> Self:
        if self.kind == "report" and not self.insights:
            raise PydanticCustomError("report_requires_insights", "Report requires insights")
        if self.kind != "report" and self.insights:
            raise PydanticCustomError("reply_forbids_insights", "Only report permits insights")
        codes = [insight.finding_code for insight in self.insights]
        if len(codes) != len(set(codes)):
            raise PydanticCustomError("duplicate_model_findings", "Each finding must appear once")
        return self


class TextPart(FrozenModel):
    type: Literal["text"] = "text"
    text: Annotated[
        str,
        StringConstraints(min_length=1, max_length=8000),
        Field(description="Prose without digits, citation templates or terminal controls."),
    ]

    @field_validator("text")
    @classmethod
    def narrative(cls, text: str) -> str:
        if any(c.isnumeric() for c in text):
            raise PydanticCustomError("raw_quantity_in_text", "Use a sourced value part")
        if "{{" in text or "}}" in text:
            raise PydanticCustomError("token_in_text_part", "Use a fact part")
        if any(unicodedata.category(c) in {"Cc", "Cf"} and c not in "\n\t" for c in text):
            raise PydanticCustomError("unsafe_text_part", "Use readable text")
        return text


class FactPart(FrozenModel):
    type: Literal["fact"] = "fact"
    ref: Alias


class ContextPart(FrozenModel):
    type: Literal["context"] = "context"
    field: Literal["company_name", "period_start", "period_end"]


class CanonicalFactPart(FrozenModel):
    type: Literal["fact"] = "fact"
    reference: Reference


ReplyPart = Annotated[TextPart | FactPart | ContextPart, Field(discriminator="type")]
CompiledPart = Annotated[TextPart | CanonicalFactPart | ContextPart, Field(discriminator="type")]


class ComposedReply(FrozenModel):
    kind: Literal["help", "clarify", "unsupported", "method", "answer", "report"]
    parts: Annotated[tuple[ReplyPart, ...], Field(min_length=1, max_length=128)]
    method: Literal["none", "runway", "synthetic", "tax", "tools"] = "none"
    insights: Annotated[tuple[ModelInsight, ...], Field(max_length=6)] = ()

    @model_validator(mode="after")
    def payload(self) -> Self:
        # Reuse the closed report invariants, without using prose as a value channel.
        ModelReply(kind=self.kind, method=self.method, text="payload", insights=self.insights)
        return self


def compose_model_reply(reply: ModelReply) -> ComposedReply:
    """Mock/fixture adapter: preserve its prose while converting citations to typed parts."""
    parts: list[ReplyPart] = []
    offset = 0
    for match in TOKEN.finditer(reply.text):
        if match.start() > offset:
            parts.append(TextPart(text=reply.text[offset : match.start()]))
        parts.append(FactPart(ref=match[1]))
        offset = match.end()
    if offset < len(reply.text):
        parts.append(TextPart(text=reply.text[offset:]))
    return ComposedReply(
        kind=reply.kind, parts=tuple(parts), method=reply.method, insights=reply.insights
    )


def canonical_composed(
    reply: ComposedReply, facts: dict[str, Fact], dataset: Dataset
) -> dict[str, Any]:
    context = {
        "company_name": dataset.company.name,
        "period_start": dataset.period_start.isoformat(),
        "period_end": dataset.period_end.isoformat(),
    }
    text = []
    parts = []
    for index, part in enumerate(reply.parts):
        if isinstance(part, TextPart):
            text.append(part.text)
            parts.append(part.model_dump(mode="json"))
        elif isinstance(part, ContextPart):
            text.append(context[part.field])
            parts.append(part.model_dump(mode="json"))
        else:
            try:
                reference = _reference(part.ref, facts)
            except GroundingError:
                raise GroundingError("UNKNOWN_FACT_ALIAS", ("parts", index, "ref")) from None
            text.append("{{fact:" + part.ref + "}}")
            parts.append({"type": "fact", "reference": reference})
    canonical = canonical_reply(
        ModelReply(
            kind=reply.kind,
            method=reply.method,
            text="".join(text),
            insights=reply.insights,
        ),
        facts,
    )
    return canonical | {"parts": parts}


def fact_aliases(results: tuple[ToolResult, ...]) -> dict[str, Fact]:
    facts = (fact for result in results if result.status == "ok" for fact in result.facts)
    return {f"f{index}": fact for index, fact in enumerate(facts)}


def _reference(alias: str, facts: dict[str, Fact]) -> dict[str, str]:
    if alias not in facts:
        raise GroundingError("UNKNOWN_FACT_ALIAS", ("text",))
    fact = facts[alias]
    return {"tool_result_id": fact.tool_result_id, "fact_id": fact.fact_id}


def canonical_reply(reply: ModelReply, facts: dict[str, Fact]) -> dict[str, Any]:
    remainder = TOKEN.sub("", reply.text)
    if "{{" in remainder or "}}" in remainder:
        raise GroundingError("INVALID_FACT_PLACEHOLDER", ("text",))
    references: list[dict[str, str]] = []
    indices: dict[str, int] = {}

    def cite(match: re.Match[str]) -> str:
        alias = match[1]
        ref = _reference(alias, facts)
        if alias not in indices:
            indices[alias] = len(references)
            references.append(ref)
        return "{{fact:" + str(indices[alias]) + "}}"

    text = TOKEN.sub(cite, reply.text)
    if "{{" in re.sub(r"\{\{fact:(0|[1-9][0-9]*)\}\}", "", text):
        raise GroundingError("INVALID_FACT_PLACEHOLDER", ("text",))
    return {
        "text": text,
        "kind": reply.kind,
        "method": reply.method,
        "references": references,
        "synthesis": {
            "insights": [
                {
                    "finding_code": insight.finding_code,
                    "action_code": insight.action_code,
                    "evidence": [_reference(alias, facts) for alias in insight.evidence],
                }
                for insight in reply.insights
            ]
        }
        if reply.kind == "report"
        else None,
    }


def model_evidence(evidence: dict[str, Any], facts: dict[str, Fact]) -> dict[str, Any]:
    by_id = {(f.tool_result_id, f.fact_id): alias for alias, f in facts.items()}
    results = []
    for result in evidence["results"]:
        rid = result["tool_result_id"]
        results.append(
            {key: value for key, value in result.items() if key not in {"tool_result_id", "facts"}}
            | {
                "facts": [
                    {key: value for key, value in fact.items() if key != "fact_id"}
                    | {"ref": by_id[(rid, fact["fact_id"])]}
                    for fact in result["facts"]
                ]
            }
        )
    options = [
        option
        | {
            "evidence_bundles": [
                [by_id[(ref["tool_result_id"], ref["fact_id"])] for ref in bundle]
                for bundle in option["evidence_bundles"]
            ]
        }
        for option in evidence["report_options"]["finding_options"]
    ]
    return evidence | {
        "results": results,
        "report_options": evidence["report_options"] | {"finding_options": options},
    }


def model_history_reply(reply: dict[str, Any], facts: dict[str, Fact]) -> str:
    by_id = {(f.tool_result_id, f.fact_id): alias for alias, f in facts.items()}
    refs = reply.get("references", [])

    def cite(match: re.Match[str]) -> str:
        ref = refs[int(match[1])]
        return "{{fact:" + by_id[(ref["tool_result_id"], ref["fact_id"])] + "}}"

    # History conveys prose and citations, never alternate JSON schemas.
    return re.sub(r"\{\{fact:(0|[1-9][0-9]*)\}\}", cite, reply["text"])


def model_reply_payload(reply: dict[str, Any], facts: dict[str, Fact]) -> dict[str, Any]:
    by_id = {(f.tool_result_id, f.fact_id): alias for alias, f in facts.items()}
    payload = {
        "kind": reply["kind"],
        "method": reply["method"],
        "text": model_history_reply(reply, facts),
        "insights": [
            insight
            | {
                "evidence": [
                    by_id[(ref["tool_result_id"], ref["fact_id"])] for ref in insight["evidence"]
                ]
            }
            for insight in (reply.get("synthesis") or {}).get("insights", [])
        ],
    }
    if reply.get("parts") is not None:
        del payload["text"]
        payload["parts"] = [
            {
                "type": "fact",
                "ref": by_id[(p["reference"]["tool_result_id"], p["reference"]["fact_id"])],
            }
            if p["type"] == "fact"
            else p
            for p in reply["parts"]
        ]
    return payload


def model_diagnostic(diagnostic: dict[str, Any], facts: dict[str, Fact]) -> dict[str, Any]:
    """Repair hints and permitted evidence use the same protocol as the writer."""
    by_id = {(f.tool_result_id, f.fact_id): alias for alias, f in facts.items()}
    issues = []
    for issue in diagnostic["validation_errors"]:
        projected = issue | {"message": MODEL_HINTS.get(issue["code"], issue["message"])}
        if "expected_evidence" in issue:
            projected["expected_evidence"] = [
                [by_id[(ref["tool_result_id"], ref["fact_id"])] for ref in bundle]
                for bundle in issue["expected_evidence"]
            ]
        issues.append(projected)
    return diagnostic | {"validation_errors": issues}

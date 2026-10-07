"""Stateful financial dialogue with model-selected tools and grounded replies."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.resources import files
from time import monotonic
from typing import Annotated, Any, Literal, Self, cast
from uuid import uuid4

from pydantic import Field, StringConstraints, model_validator
from pydantic_core import PydanticCustomError

from fintech_copilot.domain.models import Dataset, FrozenModel, Scope
from fintech_copilot.domain.results import Fact, ToolName, ToolResult
from fintech_copilot.tools.catalogue import catalogue
from fintech_copilot.tools.dispatcher import TOOLS, Dispatcher, RejectedCall

from .agent import DEFAULT_BUDGETS, Budgets, assemble_report
from .chat_plan import (
    PLAN_TOOL,
    AnalysisPlan,
    completed_results,
    definition,
    plan_state,
    validate_plan,
)
from .chat_reply import (
    CanonicalFactPart,
    CompiledPart,
    ComposedReply,
    ModelReply,
    canonical_composed,
    canonical_reply,
    compose_model_reply,
    fact_aliases,
    model_diagnostic,
    model_evidence,
    model_history_reply,
    model_reply_payload,
)
from .chat_text import (
    PLACEHOLDER,
    ChatTextError,
    normalize_text,
    number_issues,
    validate_placeholders,
    validate_text,
)
from .context import synthesis_options
from .conversation import ConversationReply, conversation_candidate, validate_conversation_text
from .diagnostics import SCHEMA_FIELDS, synthesis_diagnostic
from .grounding import GroundingError, validate_synthesis
from .models import Reference, Report, Synthesis
from .narrative_review import REVIEW_PROMPT, NarrativeReview
from .providers import Call, Provider, ProviderFailure, Turn

EVIDENCE_PREFIX = "Chat evidence:\n"
MAX_SESSION_TURNS = 20
MAX_REPLY_REFERENCES = 64


class ChatReply(FrozenModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
    kind: Literal["help", "clarify", "unsupported", "method", "answer", "report"]
    method: Literal["none", "runway", "synthetic", "tax", "tools"] = "none"
    references: Annotated[tuple[Reference, ...], Field(max_length=MAX_REPLY_REFERENCES)] = ()
    parts: Annotated[tuple[CompiledPart, ...], Field(min_length=1, max_length=128)] | None = None
    synthesis: Synthesis | None = Field(
        default=None, description="Only kind=report uses synthesis. For every other kind, use null."
    )

    @model_validator(mode="after")
    def matching_payload(self) -> Self:
        if self.kind == "answer":
            if self.synthesis is not None:
                raise PydanticCustomError("answer_forbids_synthesis", "Answer forbids synthesis")
            if not self.references:
                raise PydanticCustomError(
                    "answer_requires_references", "Answer requires references"
                )
        elif self.kind == "report":
            if self.synthesis is None:
                raise PydanticCustomError("report_requires_synthesis", "Report requires synthesis")
        elif self.references or self.synthesis is not None:
            raise PydanticCustomError(
                "nonfinancial_forbids_evidence", "Nonfinancial replies forbid financial evidence"
            )
        if self.kind == "method" and self.method == "none":
            raise PydanticCustomError("method_required", "Method reply requires a method")
        if self.kind not in {"method", "answer"} and self.method != "none":
            raise PydanticCustomError(
                "method_forbidden", "Method is not allowed for this reply kind"
            )
        return self


@dataclass
class ChatRun:
    run_id: str
    reply: ChatReply | ConversationReply | None
    facts: tuple[Fact, ...]
    report: Report | None
    error_code: str | None
    trace: list[dict[str, Any]]
    tool_results: tuple[ToolResult, ...]
    plan: AnalysisPlan | None


def chat_evidence(
    results: tuple[ToolResult, ...], dataset: Dataset, *, include_report: bool = False
) -> dict[str, Any]:
    def project_fact(fact: Fact) -> dict[str, Any]:
        projected = fact.model_dump(
            mode="json", exclude={"transaction_ids", "derivation_code", "tool_result_id"}
        )
        if fact.unit == "ids" and isinstance(fact.value, tuple) and len(fact.value) > 8:
            del projected["value"]
            projected["value_in_audit"] = True
        return projected

    return {
        "results": [
            result.model_dump(mode="json", exclude={"data", "facts"})
            | {"facts": [project_fact(fact) for fact in result.facts]}
            for result in results
            if result.status == "ok"
        ],
        "report_options": (
            synthesis_options(results, dataset.scope) if include_report else {"finding_options": []}
        ),
    }


def validate_reply(
    reply: ChatReply | ConversationReply, results: tuple[ToolResult, ...], dataset: Dataset
) -> tuple[Fact, ...]:
    if isinstance(reply, ConversationReply):
        validate_conversation_text(reply.text)
        return ()
    selected = referenced_facts(reply, results, dataset)
    if reply.parts is None:
        validate_text(reply.text, len(reply.references))
    else:
        validate_placeholders(reply.text, len(reply.references))
    return selected


def referenced_facts(
    reply: ChatReply, results: tuple[ToolResult, ...], dataset: Dataset
) -> tuple[Fact, ...]:
    """Verify actual evidence before binding any model-authored value to a reference."""
    if reply.kind == "report":
        assert reply.synthesis is not None
        validate_synthesis(reply.synthesis, results, dataset)
    facts: dict[tuple[str, str], Fact] = {}
    for result in results:
        if result.status != "ok":
            continue
        scope = Scope.model_validate(result.model_dump(include=set(Scope.model_fields)))
        dataset.select(scope)
        if catalogue(dataset, scope, result.tool_result_id, result.data) != result.facts:
            raise GroundingError("FACT_DATA_MISMATCH")
        facts.update({(f.tool_result_id, f.fact_id): f for f in result.facts})
    selected: list[Fact] = []
    seen: set[tuple[str, str]] = set()
    for index, ref in enumerate(reply.references):
        key = (ref.tool_result_id, ref.fact_id)
        if key not in facts or key in seen:
            raise GroundingError(
                "FABRICATED_STALE_OR_DUPLICATE_REFERENCE",
                ("references", index),
                "DUPLICATE_REFERENCE" if key in seen else "UNKNOWN_FACT_REFERENCE",
            )
        seen.add(key)
        selected.append(facts[key])
    if reply.parts is not None:
        for index, part in enumerate(reply.parts):
            if (
                isinstance(part, CanonicalFactPart)
                and (part.reference.tool_result_id, part.reference.fact_id) not in facts
            ):
                raise GroundingError("UNKNOWN_FACT_REFERENCE", ("parts", index, "reference"))
        aliases = fact_aliases(results)
        compiled = canonical_composed(
            ComposedReply.model_validate(
                model_reply_payload(reply.model_dump(mode="json"), aliases)
            ),
            aliases,
            dataset,
        )
        if compiled != reply.model_dump(mode="json"):
            raise GroundingError("COMPOSITION_MISMATCH", ("parts",))
    return tuple(selected)


class ChatSession:
    def __init__(
        self, dataset: Dataset, provider: Provider, budgets: Budgets = DEFAULT_BUDGETS
    ) -> None:
        self.dataset, self.provider, self.budgets = dataset, provider, budgets
        self.dispatcher = Dispatcher(dataset)
        self.results: dict[str, ToolResult] = {}
        self.seen_call_ids: set[str] = set()
        self.turn_count = 0
        self.last_failure: dict[str, Any] | None = None
        self.last_plan: AnalysisPlan | None = None
        resources = files("fintech_copilot.resources")
        self.definitions = json.loads(resources.joinpath("tools.json").read_text())
        self.definitions.append(definition())
        self.messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": resources.joinpath("chat-system.md").read_text(),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "company": dataset.company.model_dump(mode="json"),
                        "scope": dataset.scope.model_dump(mode="json"),
                    },
                    ensure_ascii=False,
                ),
            },
        ]

    async def send(
        self, question: str, trace_sink: Callable[[dict[str, Any]], None] | None = None
    ) -> ChatRun:
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 4096:
            raise ValueError("Message requires 1..4096 characters")
        if self.turn_count >= MAX_SESSION_TURNS:
            raise ValueError("Session limit reached; reset the dialogue")
        self.turn_count += 1
        started = monotonic()
        run_id = f"run:{uuid4().hex}"
        messages = self._history() + [{"role": "user", "content": question.strip()}]
        trace: list[dict[str, Any]] = []
        plan: AnalysisPlan | None = None
        evidence_message: dict[str, Any] | None = None

        def emit(event: str, **details: Any) -> None:
            item = {
                "run_id": run_id,
                "event_index": len(trace),
                "event": event,
                "timestamp": datetime.now(UTC).isoformat(),
                "provider_mode": self.provider.mode,
                "model": self.provider.model,
                **details,
            }
            trace.append(item)
            if trace_sink:
                trace_sink(item)

        def add_evidence() -> None:
            nonlocal evidence_message
            if evidence_message is not None:
                messages[:] = [m for m in messages if m is not evidence_message]
            evidence = chat_evidence(
                tuple(self.results.values()),
                self.dataset,
                include_report=plan is not None and plan.mode == "report",
            )
            evidence = model_evidence(evidence, fact_aliases(tuple(self.results.values())))
            evidence["session_failure"] = self.last_failure
            evidence["previous_tools"] = (
                [step.tool for step in self.last_plan.steps] if self.last_plan else []
            )
            if plan:
                evidence["plan_mode"] = plan.mode
                evidence["plan_progress"] = plan_state(plan, tuple(self.results.values()))
                evidence["final_reply_rules"] = (
                    "After all steps complete, return ComposedReply with kind=answer, method=none, "
                    "insights=[] and parts citing facts for every step in order."
                    if plan.mode == "answer"
                    else "After all steps complete, return ComposedReply "
                    "with kind=report, method=none, "
                    "parts citing facts in step order and insights from report_options. "
                    "Each finding_code may appear only once."
                ) + (
                    " If the request cannot be completed, "
                    "explain using kind=clarify or unsupported."
                )
            evidence_message = {
                "role": "user",
                "content": EVIDENCE_PREFIX + json.dumps(evidence),
            }
            messages.append(evidence_message)

        emit(
            "run_started",
            company_id=self.dataset.company.company_id,
            snapshot_id=self.dataset.snapshot_id,
            session_turn=self.turn_count,
        )
        if self.results:
            add_evidence()
        reply: ChatReply | ConversationReply | None = None
        selected: tuple[Fact, ...] = ()
        report = None
        error = None
        calls = repairs = 0
        spent_turns = 0
        repair_requires_tools = False
        feedback_message: str | None = None
        try:
            for _ in range(self.budgets.provider_turns):
                turn_index = spent_turns
                plan_complete = plan is not None and all(
                    result is not None
                    for result in completed_results(plan, tuple(self.results.values()))
                )
                json_only = not repair_requires_tools and (
                    plan_complete or (plan is None and repairs > 0)
                )
                request_messages = (
                    self._final_messages(
                        question, evidence_message, feedback_message, financial=plan is not None
                    )
                    if json_only
                    else messages
                )
                response = await self._complete(
                    request_messages, json_only, started, turn_index, emit
                )
                spent_turns += 1
                messages.append(response.message())
                emit(
                    "provider_turn",
                    turn=turn_index,
                    status="ok",
                    tool_call_count=len(response.calls),
                    finish_reason=response.finish_reason,
                    usage=asdict(response.usage) if response.usage else None,
                )
                if response.calls:
                    for call in response.calls:
                        calls += 1
                        if calls > self.budgets.tool_calls:
                            raise ProviderFailure("TOOL_CALL_LIMIT")
                        if monotonic() - started >= self.budgets.deadline:
                            raise ProviderFailure("RUN_DEADLINE")
                        if call.name == PLAN_TOOL:
                            previous = plan
                            plan = self._plan(call, plan, messages, emit)
                            if plan:
                                self.last_plan = plan
                            if plan is not None and plan != previous:
                                repair_requires_tools = False
                        else:
                            self._dispatch(call, messages, self.seen_call_ids, emit, plan)
                    add_evidence()
                    continue
                review_issues: tuple[str, ...] = ()
                candidate: ChatReply | ConversationReply | None = None
                repair_candidate: dict[str, Any] | None = None
                try:
                    if plan is None:
                        candidate = conversation_candidate(response.content or "", self.dataset)
                    if isinstance(candidate, ConversationReply):
                        selected = ()
                        repair_candidate = candidate.model_dump(mode="json")
                    else:
                        try:
                            raw = json.loads(response.content or "")
                        except json.JSONDecodeError:
                            ComposedReply.model_validate_json(response.content or "")
                            raise  # defensive: invalid JSON is rejected above
                        if isinstance(raw, dict) and ("references" in raw or "synthesis" in raw):
                            candidate = ChatReply.model_validate(raw)
                        elif isinstance(raw, dict) and raw.get("parts") is not None:
                            repair_candidate = {
                                k: raw[k] for k in ComposedReply.model_fields if k in raw
                            }
                            if len(json.dumps(repair_candidate)) > 32768:
                                repair_candidate = None
                            composed = ComposedReply.model_validate(raw)
                            repair_candidate = composed.model_dump(mode="json")
                            candidate = ChatReply.model_validate(
                                canonical_composed(
                                    composed,
                                    fact_aliases(tuple(self.results.values())),
                                    self.dataset,
                                )
                            )
                        else:
                            draft = ModelReply.model_validate(raw)
                            repair_candidate = draft.model_dump(mode="json")
                            candidate = ChatReply.model_validate(
                                canonical_reply(draft, fact_aliases(tuple(self.results.values())))
                            )
                        if candidate.kind in {"answer", "report"}:
                            pending = plan_state(plan, tuple(self.results.values())) if plan else []
                            if plan is None or any(s["status"] == "pending" for s in pending):
                                emit(
                                    "plan_incomplete",
                                    steps=pending,
                                    error_code="MISSING_PLAN"
                                    if plan is None
                                    else "PLAN_INCOMPLETE",
                                )
                                messages.append(
                                    {
                                        "role": "user",
                                        "content": "Do not finish yet. PlanAnalysis is required "
                                        "before a financial "
                                        "answer. Finish all pending steps using tools:\n"
                                        + json.dumps(pending),
                                    }
                                )
                                continue
                        selected = referenced_facts(
                            candidate, tuple(self.results.values()), self.dataset
                        )
                        normalized = (
                            normalize_text(candidate.text, selected)
                            if candidate.parts is None
                            else None
                        )
                        if normalized is not None and normalized.text != candidate.text:
                            candidate = ChatReply.model_validate(
                                candidate.model_dump() | {"text": normalized.text}
                            )
                            emit(
                                "reply_normalized",
                                fact_bindings=len(normalized.fact_bindings),
                                list_labels=normalized.list_labels,
                            )
                        repair_candidate = model_reply_payload(
                            candidate.model_dump(mode="json"),
                            fact_aliases(tuple(self.results.values())),
                        )
                        if candidate.parts is None:
                            validate_text(candidate.text, len(candidate.references))
                        else:
                            validate_placeholders(candidate.text, len(candidate.references))
                        if candidate.kind in {"answer", "report"} and plan:
                            self._validate_plan_answer(plan, candidate, selected)
                        if plan and candidate.kind not in {
                            "answer",
                            "report",
                            "clarify",
                            "unsupported",
                        }:
                            raise GroundingError("INCOMPLETE_PLAN_ANSWER", ("kind",))
                    if isinstance(candidate, ConversationReply) or plan or self.results:
                        review_messages = [
                            {
                                "role": "system",
                                "content": REVIEW_PROMPT
                                + "\nReview schema:\n"
                                + json.dumps(NarrativeReview.model_json_schema()),
                            },
                            {
                                "role": "user",
                                "content": json.dumps(
                                    {
                                        "question": question,
                                        "dialogue_history": self._history()[2:],
                                        "dialogue_mode": (
                                            "conversation"
                                            if isinstance(candidate, ConversationReply)
                                            else "financial"
                                        ),
                                        "capabilities": list(TOOLS),
                                        "plan": plan.model_dump(mode="json") if plan else None,
                                        "session_failure": self.last_failure,
                                        "company": self.dataset.company.model_dump(mode="json"),
                                        "dataset_scope": self.dataset.scope.model_dump(mode="json"),
                                        "candidate": candidate.model_dump(mode="json"),
                                        "evidence": chat_evidence(
                                            ()
                                            if isinstance(candidate, ConversationReply)
                                            else tuple(self.results.values()),
                                            self.dataset,
                                            include_report=plan is not None
                                            and plan.mode == "report",
                                        ),
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        ]
                        review_turn = await self._complete(
                            review_messages, True, started, spent_turns, emit
                        )
                        emit(
                            "provider_turn",
                            turn=spent_turns,
                            phase="narrative_review",
                            status="ok",
                            tool_call_count=len(review_turn.calls),
                            finish_reason=review_turn.finish_reason,
                            usage=asdict(review_turn.usage) if review_turn.usage else None,
                        )
                        spent_turns += 1
                        if review_turn.calls:
                            raise ProviderFailure("INVALID_REVIEW")
                        try:
                            review = NarrativeReview.model_validate_json(review_turn.content or "")
                        except ValueError:
                            raise ProviderFailure("INVALID_REVIEW") from None
                        emit(
                            "narrative_review",
                            supported=review.supported,
                            issues=list(review.issues),
                        )
                        if not review.supported:
                            review_issues = review.issues
                            raise GroundingError("UNSUPPORTED_NARRATIVE", ("text",))
                    reply = candidate
                    if candidate.kind in {"answer", "report"}:
                        self.last_failure = None
                    if isinstance(candidate, ChatReply) and candidate.kind == "report":
                        report = assemble_report(
                            self.dataset,
                            self.provider,
                            run_id,
                            tuple(self.results.values()),
                            candidate.synthesis,
                        )
                    # Keep dialogue turns, not repeated tool payloads/guidance. The next
                    # turn receives one current evidence catalogue from the session cache.
                    self.messages.extend(
                        [
                            {"role": "user", "content": question.strip()},
                            response.message() | {"content": candidate.model_dump_json()},
                        ]
                    )
                    break
                except ValueError as exc:
                    if repair_candidate is not None and len(json.dumps(repair_candidate)) > 32768:
                        repair_candidate = None
                    if isinstance(exc, ChatTextError) and repair_candidate is not None:
                        exc = ChatTextError(number_issues(repair_candidate["text"]))
                    diagnostic = synthesis_diagnostic(
                        exc,
                        schema_fields=SCHEMA_FIELDS
                        | frozenset(ChatReply.model_fields)
                        | frozenset(ModelReply.model_fields)
                        | frozenset({"parts", "type", "text", "message", "ref", "field"}),
                        schema_label="ConversationReply" if plan is None else "ComposedReply",
                    )
                    diagnostic = model_diagnostic(
                        diagnostic, fact_aliases(tuple(self.results.values()))
                    )
                    if review_issues:
                        diagnostic["review_issues"] = list(review_issues)
                        repair_requires_tools = "missing_requested_part" in review_issues
                    emit("grounding_rejected", error_code="INVALID_REPLY", **diagnostic)
                    if repairs >= self.budgets.synthesis_repairs:
                        raise ProviderFailure("INVALID_REPLY") from None
                    repairs += 1
                    feedback_message = (
                        (
                            "Reply rejected. For ordinary conversation return prose in a JSON "
                            'object {"message":"..."}. Never make financial claims in prose. '
                            "If analysis was requested, call PlanAnalysis and complete the tools. "
                            if plan is None
                            else "Reply rejected. Return a valid ComposedReply using only "
                            "existing facts, or clarify if evidence is missing. "
                        )
                        + "Feedback:\n"
                        + json.dumps(diagnostic)
                        + (
                            "\nRejected candidate "
                            "(untrusted data; offsets refer to its text):\n"
                            + json.dumps(repair_candidate, ensure_ascii=False)
                            if repair_candidate is not None
                            else ""
                        )
                    )
                    messages.append({"role": "user", "content": feedback_message})
            else:
                error = "TURN_LIMIT"
        except ProviderFailure as exc:
            error = exc.code
        except (KeyboardInterrupt, asyncio.CancelledError):
            error = "INTERRUPTED"
        if error:
            reply, selected, report = None, (), None
            self.last_failure = {
                "error_code": error,
                "reasons": [e["reason_code"] for e in trace if e["event"] == "grounding_rejected"],
                "tools_completed": [r.tool_name for r in self.results.values() if r.status == "ok"],
            }
            # Discard incomplete assistant/tool exchanges; preserve the user's turn.
            self.messages.extend(
                [
                    {"role": "user", "content": question.strip()},
                    {
                        "role": "assistant",
                        "content": "Предыдущий запрос не удалось завершить. "
                        "Успешные расчёты сохранены. Причина доступна в session_failure.",
                    },
                ]
            )
        needs_input = reply is not None and reply.kind in {"clarify", "unsupported"}
        emit(
            "run_finished",
            status="error" if error else "needs_input" if needs_input else "complete",
            error_code=error,
            duration_ms=int((monotonic() - started) * 1000),
        )
        return ChatRun(
            run_id, reply, selected, report, error, trace, tuple(self.results.values()), plan
        )

    def _history(self) -> list[dict[str, Any]]:
        facts = fact_aliases(tuple(self.results.values()))
        messages = list(self.messages[:2])
        for message in self.messages[2:]:
            if message["role"] != "assistant":
                messages.append(message)
                continue
            try:
                reply = ChatReply.model_validate_json(message["content"])
            except ValueError:
                messages.append(message)
            else:
                messages.append(
                    message | {"content": model_history_reply(reply.model_dump(mode="json"), facts)}
                )
        return messages

    def _final_messages(
        self,
        question: str,
        evidence: dict[str, Any] | None,
        feedback: str | None,
        *,
        financial: bool,
    ) -> list[dict[str, Any]]:
        resources = files("fintech_copilot.resources")
        history = self._history()
        messages = [
            {
                "role": "system",
                "content": (
                    resources.joinpath("chat-final.md").read_text()
                    + "\nComposedReply JSON schema:\n"
                    + json.dumps(ComposedReply.model_json_schema())
                    if financial
                    else resources.joinpath("chat-system.md").read_text()
                    + '\nConversation repair: return JSON {"message":"your ordinary reply"}. '
                    + "If financial analysis is required, request PlanAnalysis instead."
                ),
            },
            history[1],
        ]
        messages.extend({"role": m["role"], "content": m["content"]} for m in history[2:])
        messages.append({"role": "user", "content": question.strip()})
        if evidence:
            messages.append(evidence)
        else:
            messages.append(
                {
                    "role": "user",
                    "content": EVIDENCE_PREFIX
                    + json.dumps(
                        {
                            "results": [],
                            "report_options": {"finding_options": []},
                            "session_failure": self.last_failure,
                        }
                    ),
                }
            )
        if feedback:
            messages.append({"role": "user", "content": feedback})
        return messages

    def _plan(
        self,
        call: Call,
        previous: AnalysisPlan | None,
        messages: list[dict[str, Any]],
        emit: Callable[..., None],
    ) -> AnalysisPlan | None:
        emit("tool_call", tool=PLAN_TOOL, tool_call_id=call.id, arguments=None)
        output: dict[str, Any]
        chosen: AnalysisPlan | None
        try:
            if not call.id or call.id in self.seen_call_ids or len(call.arguments.encode()) > 8192:
                raise PydanticCustomError("invalid_plan_call", "Invalid plan call")
            self.seen_call_ids.add(call.id)
            plan = AnalysisPlan.model_validate_json(call.arguments)
            validate_plan(plan, self.dataset, previous)
            for index, step in enumerate(plan.steps):
                try:
                    validate_text(step.task, 0)
                except GroundingError as exc:
                    exc.path = ("steps", index, "task")
                    raise
        except ValueError as exc:
            diagnostic = synthesis_diagnostic(
                exc,
                schema_fields=frozenset(
                    {"mode", "steps", "task", "tool", "scope", *Scope.model_fields}
                ),
                schema_label="AnalysisPlan",
            )
            output = {
                "status": "error",
                "error": {
                    "code": "INVALID_PLAN",
                    "message": "Use valid unique tool/scopes, include all full-report tools, "
                    "and preserve "
                    "all previous steps. Task titles must not contain numbers.",
                    "feedback": diagnostic,
                },
            }
            emit(
                "tool_result",
                tool=PLAN_TOOL,
                tool_call_id=call.id,
                status="error",
                error_code="INVALID_PLAN",
                **diagnostic,
            )
            chosen = previous
        else:
            state = plan_state(plan, tuple(self.results.values()))
            output = {"status": "ok", "mode": plan.mode, "steps": state}
            emit("plan_updated", mode=plan.mode, steps=state)
            emit("tool_result", tool=PLAN_TOOL, tool_call_id=call.id, status="ok")
            chosen = plan
        messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(output)})
        return chosen

    def _validate_plan_answer(
        self, plan: AnalysisPlan, reply: ChatReply, selected: tuple[Fact, ...]
    ) -> None:
        if (reply.kind == "report") != (plan.mode == "report"):
            raise GroundingError("INCOMPLETE_PLAN_ANSWER", ("kind",))
        results = completed_results(plan, tuple(self.results.values()))
        required = [r.tool_result_id for r in results if r is not None]
        mentioned = [
            selected[int(m.group(1))].tool_result_id for m in PLACEHOLDER.finditer(reply.text)
        ]
        first_positions = [mentioned.index(rid) if rid in mentioned else -1 for rid in required]
        if any(p < 0 for p in first_positions) or first_positions != sorted(first_positions):
            raise GroundingError("INCOMPLETE_PLAN_ANSWER", ("references",))

    async def _complete(
        self,
        messages: list[dict[str, Any]],
        repairing: bool,
        started: float,
        turn_index: int,
        emit: Callable[..., None],
    ) -> Turn:
        if turn_index >= self.budgets.provider_turns:
            raise ProviderFailure("TURN_LIMIT")
        for attempt in range(2):
            remaining = self.budgets.deadline - (monotonic() - started)
            if remaining <= 0:
                raise ProviderFailure("RUN_DEADLINE")
            timeout = min(self.budgets.attempt_timeout, remaining)
            emit("provider_started", turn=turn_index, attempt=attempt)
            try:
                return await asyncio.wait_for(
                    self.provider.complete(
                        messages, [] if repairing else self.definitions, timeout
                    ),
                    timeout=timeout,
                )
            except TimeoutError:
                failure = ProviderFailure("PROVIDER_TIMEOUT", transient=True)
            except ProviderFailure as exc:
                failure = exc
            except Exception:
                failure = ProviderFailure("PROVIDER_ERROR")
            emit(
                "provider_turn",
                turn=turn_index,
                attempt=attempt,
                status="error",
                error_code=failure.code,
            )
            if not failure.transient or attempt:
                raise failure
            if self.budgets.deadline - (monotonic() - started) <= self.budgets.retry_backoff:
                raise ProviderFailure("RUN_DEADLINE")
            await asyncio.sleep(self.budgets.retry_backoff)
        raise ProviderFailure("PROVIDER_ERROR")  # defensive: attempts either return or raise

    def _dispatch(
        self,
        call: Call,
        messages: list[dict[str, Any]],
        seen: set[str],
        emit: Callable[..., None],
        plan: AnalysisPlan | None,
    ) -> None:
        safe_args = None
        cached = False
        try:
            scope = Scope.model_validate_json(call.arguments)
            self.dataset.select(scope)
            safe_args = scope.model_dump(mode="json")
            cached = (call.name, scope) in self.dispatcher.cache
        except ValueError:
            pass
        emit(
            "tool_call",
            tool=call.name if call.name in TOOLS else "unknown",
            tool_call_id=call.id,
            arguments=safe_args,
        )
        if not call.id or call.id in seen:
            result: ToolResult | RejectedCall = RejectedCall(message="Duplicate or empty call ID")
        else:
            seen.add(call.id)
            allowed = False
            if plan and safe_args:
                for step, completed in zip(
                    plan.steps, completed_results(plan, tuple(self.results.values())), strict=True
                ):
                    if step.tool == call.name and step.scope.model_dump(mode="json") == safe_args:
                        allowed = True
                        break
                    if completed is None:
                        break
            result = (
                self.dispatcher.dispatch(call.name, call.arguments)
                if allowed
                else RejectedCall(message="Plan this tool/scope first and follow step order")
            )
        if isinstance(result, RejectedCall):
            output = result.model_dump_json()
            emit("tool_result", tool_call_id=call.id, status="error", error_code=result.code)
        else:
            self.results[result.tool_result_id] = result
            output = (
                json.dumps(
                    result.model_dump(mode="json", exclude={"data", "facts"})
                    | {"facts_in_current_evidence": True}
                )
                if result.status == "ok"
                else result.model_dump_json()
            )
            emit(
                "tool_result",
                tool=call.name,
                tool_call_id=call.id,
                tool_result_id=result.tool_result_id,
                status=result.status,
                cache_hit=cached,
                error_code=result.error.code if result.status == "error" else None,
            )
        messages.append({"role": "tool", "tool_call_id": call.id, "content": output})


class MockChatProvider:
    """Scripted offline emulator; plans and actual tools use the live protocol."""

    mode: Literal["mock", "live"] = "mock"
    model: str | None = None

    def __init__(self) -> None:
        self.call_index = 0

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], timeout: float
    ) -> Turn:
        from decimal import Decimal

        from .chat_plan import AnalysisStep
        from .chat_reply import ModelInsight

        def finish(reply: ModelReply) -> Turn:
            return Turn(content=compose_model_reply(reply).model_dump_json())

        if messages[0]["content"].startswith(REVIEW_PROMPT):
            return Turn(content=NarrativeReview(supported=True).model_dump_json())
        context = json.loads(messages[1]["content"])
        scope = Scope.model_validate(context["scope"])
        evidence: dict[str, Any] = {"results": [], "report_options": {"finding_options": []}}
        question = ""
        planned = False
        for message in messages[2:]:
            if message["role"] == "tool":
                raw = json.loads(message["content"])
                if raw.get("status") == "ok" and "steps" in raw:
                    planned = True
            if message["role"] != "user":
                continue
            content = message["content"]
            if content.startswith(EVIDENCE_PREFIX):
                evidence = json.loads(content[len(EVIDENCE_PREFIX) :])
                planned = planned or bool(evidence.get("plan_progress"))
            elif not content.startswith(
                ("Reply rejected.", "Plan progress:", "Do not finish yet.")
            ):
                question = content.casefold()
        full = any(w in question for w in ("отчет", "отчёт", "report"))
        if any(w in question for w in ("синтет", "генератор")):
            return finish(
                ModelReply(
                    kind="method",
                    method="synthetic",
                    text="Это данные из заранее настроенных сценариев. "
                    "Меняются даты и отдельные платежи, "
                    "а базовые месячные обороты и доли категорий сохраняются. Реальных счетов и "
                    "контрагентов здесь нет.",
                )
            )
        if "как" in question and any(w in question for w in ("счит", "работает")):
            return finish(
                ModelReply(
                    kind="method",
                    method="runway",
                    text="Runway связывает остаток денег со средним месячным чистым burn. Burn — "
                    "операционные выплаты за вычетом поступлений. Это оценка по прошлым движениям, "
                    "а не обещание того, что будущие платежи будут такими же.",
                )
            )
        keywords = [
            (TOOLS[0], ("runway", "запас", "ликвид", "остаток", "денег", "баланс", "кассов")),
            (TOOLS[1], ("расход", "opex")),
            (TOOLS[2], ("аномал", "платеж", "платёж")),
            (TOOLS[3], ("налог", "tax")),
        ]
        found = [
            (min(question.index(w) for w in words if w in question), name)
            for name, words in keywords
            if any(w in question for w in words)
        ]
        required = list(TOOLS) if full else [name for _, name in sorted(found)]
        if not required and any(w in question for w in ("почему", "подроб", "why")):
            required = list(dict.fromkeys(evidence.get("previous_tools", [])))
        if not required:
            greeting = any(w in question for w in ("привет", "hello", "помощ", "умеешь"))
            return finish(
                ModelReply(
                    kind="help" if greeting else "clarify",
                    text="Привет! Могу разобраться с расходами, ликвидностью "
                    "и необычными платежами "
                    "компании. С чего начнём?"
                    if greeting
                    else "Уточни, пожалуйста, что хочешь проверить. "
                    "Могу разобрать расходы, денежные "
                    "потоки, аномалии или подготовить полный отчёт.",
                )
            )
        index = self.call_index
        self.call_index += 1
        if not planned:
            titles = {
                TOOLS[0]: "Проверить ликвидность",
                TOOLS[1]: "Разобрать расходы",
                TOOLS[2]: "Проверить аномалии",
                TOOLS[3]: "Проверить налоговые документы",
            }
            plan = AnalysisPlan(
                mode="report" if full else "answer",
                steps=tuple(
                    AnalysisStep(task=titles[name], tool=cast(ToolName, name), scope=scope)
                    for name in required
                ),
            )
            return Turn(calls=(Call(f"chat-plan-{index}", PLAN_TOOL, plan.model_dump_json()),))
        available = {
            r["tool_name"]: r
            for r in evidence["results"]
            if all(r.get(k) == v for k, v in context["scope"].items())
        }
        missing = [name for name in required if name not in available]
        if missing:
            return Turn(
                calls=tuple(
                    Call(f"chat-mock-{index}-{i}", name, scope.model_dump_json())
                    for i, name in enumerate(missing)
                )
            )
        paragraphs: list[str] = []

        def cite(result: dict[str, Any], fact: dict[str, Any]) -> str:
            return "{{fact:" + str(fact["ref"]) + "}}"

        for name in required:
            result = available[name]
            facts = {f["metric"]: f for f in result["facts"]}
            if name == TOOLS[0]:
                runway = cite(result, facts["runway_months"])
                cash = cite(result, facts["closing_cash"])
                burn = cite(result, facts["average_net_burn"])
                paragraphs.append(
                    f"По прошлым денежным потокам runway — {runway}. Остаток денег "
                    f"— {cash}, средний месячный чистый burn — {burn}. Это "
                    "ретроспективная оценка, а не прогноз будущих оплат."
                )
            elif name == TOOLS[1]:
                total = cite(result, facts["known_opex_total"])
                rows = [
                    f
                    for f in result["facts"]
                    if f["metric"].startswith("category:") and f["metric"].endswith(":total")
                ]
                largest = max(rows, key=lambda f: Decimal(f["value"]))
                amount = cite(result, largest)
                share = cite(
                    result, facts[largest["metric"].removesuffix(":total") + ":share_percent"]
                )
                label = {
                    "rd_contractors": "R&D и подрядчики",
                    "subcontractors": "субподрядчики",
                    "logistics": "логистика",
                    "rent": "аренда",
                    "fuel": "топливо",
                    "cloud_services": "облачные сервисы",
                }.get(largest["metric"].split(":")[1], "категория")
                paragraphs.append(
                    f"Начну с расходов. Классифицированные операционные расходы "
                    f"составили {total}. Наибольшая статья — {label}: {amount}, "
                    f"или {share} от известных OPEX. Закупки запасов и налоговые "
                    "платежи в этот показатель автоматически не включаются."
                )
            elif name == TOOLS[2]:
                amounts = [
                    f
                    for f in result["facts"]
                    if f["metric"].startswith("anomaly:") and f["metric"].endswith(":amount")
                ]
                if amounts:
                    fact = amounts[0]
                    prefix = fact["metric"].removesuffix(":amount")
                    amount = cite(result, fact)
                    baseline = cite(result, facts[prefix + ":baseline_amount"])
                    threshold = cite(result, facts[prefix + ":threshold_amount"])
                    paragraphs.append(
                        f"Теперь аномалии. Инструмент отметил отклонение: {amount}, "
                        f"базовое значение — {baseline}, порог — {threshold}. "
                        "Стоит проверить состав платежей и документы. По "
                        "этому сигналу причину списаний установить нельзя."
                    )
                else:
                    cross = facts["unclassified_cross_border_total"]
                    if Decimal(cross["value"]) > 0:
                        amount = cite(result, cross)
                        paragraphs.append(
                            f"По аномалиям есть неклассифицированные трансграничные "
                            f"выплаты на {amount}. Я бы запросил назначение и "
                            "подтверждающие документы; это не доказательство нарушения."
                        )
                    else:
                        ids = cite(result, facts["anomaly_ids"])
                        paragraphs.append(
                            f"Аномалии по правилам детектора: {ids}. Это проверка "
                            "по доступной истории, а не гарантия отсутствия рисков."
                        )
            else:
                review = cite(result, facts["tax:review_required:total"])
                paragraphs.append(
                    f"В налоговом драфте на ручную проверку отнесены расходы на "
                    f"{review}. Сумма налога не рассчитана — нужны документы и "
                    "полный учётный период."
                )
        insights: tuple[ModelInsight, ...] = ()
        if full:
            insights = tuple(
                ModelInsight(
                    finding_code=o["finding_code"],
                    action_code=o["allowed_actions"][0],
                    evidence=o["evidence_bundles"][0],
                )
                for o in evidence["report_options"]["finding_options"][:6]
            )
        return Turn(
            content=compose_model_reply(
                ModelReply(
                    kind="report" if full else "answer",
                    text="\n\n".join(paragraphs),
                    insights=insights,
                )
            ).model_dump_json()
        )

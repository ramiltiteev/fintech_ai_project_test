"""Ordinary dialogue is provisional prose, never a financial tool result."""

from __future__ import annotations

import json
import unicodedata
from typing import Annotated, Any, Literal, Self

from pydantic import Field, StringConstraints, model_validator
from pydantic_core import PydanticCustomError

from fintech_copilot.domain.models import Dataset, FrozenModel

from .chat_reply import ContextPart
from .grounding import GroundingError

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
Method = Literal["none", "runway", "synthetic", "tax", "tools"]


class ConversationReply(FrozenModel):
    text: Text
    kind: Literal["help", "clarify", "unsupported", "method"] = "help"
    method: Method = "none"
    references: Annotated[tuple[()], Field(max_length=0)] = ()
    synthesis: None = None
    parts: None = None

    @model_validator(mode="after")
    def safe_prose(self) -> Self:
        validate_conversation_text(self.text)
        if (self.kind == "method") != (self.method != "none"):
            raise PydanticCustomError("method_required", "Method and kind must agree")
        return self


class _TextPart(FrozenModel):
    type: Literal["text"]
    text: Annotated[str, StringConstraints(min_length=1, max_length=8000)]


class _Envelope(FrozenModel):
    kind: Literal["help", "clarify", "unsupported", "method", "answer", "report"] = "help"
    method: Method = "none"
    text: Text | None = None
    message: Text | None = None
    parts: Annotated[tuple[_TextPart | ContextPart, ...], Field(max_length=128)] | None = None
    references: Annotated[tuple[()], Field(max_length=0)] = ()
    synthesis: None = None
    insights: Annotated[tuple[()], Field(max_length=0)] = ()

    @model_validator(mode="after")
    def one_body(self) -> Self:
        if sum(body is not None for body in (self.text, self.message, self.parts)) != 1:
            raise PydanticCustomError("conversation_body_required", "Use one conversation body")
        return self


def validate_conversation_text(text: str) -> None:
    if any(unicodedata.category(c) in {"Cc", "Cf"} and c not in "\n\t" for c in text):
        raise GroundingError("UNSAFE_CHAT_TEXT", ("text",))
    if "{{" in text or "}}" in text:
        raise GroundingError("INVALID_FACT_PLACEHOLDER", ("text",))


def conversation_candidate(content: str, dataset: Dataset) -> ConversationReply | None:
    """Parse a source-free draft; the caller must review its intent and claims."""
    try:
        raw: Any = json.loads(content)
    except json.JSONDecodeError:
        if content.lstrip().startswith(("{", "[", "```")):
            return None
        return ConversationReply(text=content)
    if not isinstance(raw, dict):
        return None
    parts = raw.get("parts")
    if (
        raw.get("references")
        or raw.get("synthesis")
        or raw.get("insights")
        or (
            isinstance(parts, list)
            and any(isinstance(part, dict) and part.get("type") == "fact" for part in parts)
        )
    ):
        return None
    draft = _Envelope.model_validate(raw)
    context = {
        "company_name": dataset.company.name,
        "period_start": dataset.period_start.isoformat(),
        "period_end": dataset.period_end.isoformat(),
    }
    text = (
        "".join(context[p.field] if isinstance(p, ContextPart) else p.text for p in draft.parts)
        if draft.parts is not None
        else draft.text or draft.message
    )
    assert text is not None  # one_body guarantees a body
    kind: Literal["help", "clarify", "unsupported", "method"] = "help"
    if draft.kind == "clarify":
        kind = "clarify"
    elif draft.kind == "unsupported":
        kind = "unsupported"
    elif draft.kind == "method":
        kind = "method"
    return ConversationReply(text=text, kind=kind, method=draft.method)

"""Narrative values use references, never model-authored financial numbers."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from fintech_copilot.domain.results import Fact

from .grounding import GroundingError

PLACEHOLDER = re.compile(r"\{\{fact:(0|[1-9][0-9]*)\}\}")
NUMBER_WORD = re.compile(
    r"\b(?:ноль|нул[ьяюеи]|один|одна|одно|одного|одной|одним|одному|одну|два|две|двух|двумя|"
    r"три|трёх|трех|четыр\w*|пять|пяти|шесть|шести|семь|семи|восемь|восьми|девять|девяти|"
    r"десять|десяти|одиннадцат\w*|двенадцат\w*|тринадцат\w*|четырнадцат\w*|пятнадцат\w*|"
    r"шестнадцат\w*|семнадцат\w*|восемнадцат\w*|девятнадцат\w*|двадцат\w*|тридцат\w*|"
    r"сорок\w*|пятьдесят|шестьдесят|семьдесят|восемьдесят|девяносто|сто|сот(?:ня|ни|ню|ен|не|ней|ням|нями)|"
    r"двести|трист\w*|четырест\w*|пятьсот|шестьсот|семьсот|восемьсот|девятьсот|"
    r"тысяч\w*|миллион\w*|миллиард\w*|половин\w*|четверть|"
    r"zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|"
    r"forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion|half)\b",
    re.IGNORECASE,
)

IDIOM = re.compile(r"\bс\s+одной\s+стороны\b", re.IGNORECASE)
SINGLETON = re.compile(r"(?:один|одна|одно|одного|одной|одним|одному|одну|one)", re.IGNORECASE)
QUALITATIVE_SINGLETON = re.compile(
    r"\s+(?:из|of|проблем\w*|шаг\w*|вопрос\w*|пример\w*|момент\w*|причин\w*|"
    r"аспект\w*|уточнени\w*|problem|step|question|example|aspect)\b",
    re.IGNORECASE,
)


def _quantitative_word(match: re.Match[str], text: str) -> bool:
    singleton = SINGLETON.fullmatch(match.group()) is not None
    right = text[match.end() : match.end() + 80]
    # Narrow qualitative exceptions; monetary/count/duration words remain blocked
    # even when a unit is omitted or phrased in a way the regex does not recognise.
    return not (singleton and QUALITATIVE_SINGLETON.match(right))


LIST_LABEL = re.compile(
    r"^(?P<prefix>[ \t]*(?:\#{1,6}[ \t]+)?)(?P<label>[1-9][0-9]{0,2})[.)][ \t]+",
    re.MULTILINE,
)
SPACE = r"[ \u00a0\u202f]"
RAW_NUMBER = r"[+\-−]?[0-9](?:[0-9., \u00a0\u202f]*[0-9])?"
UNIT = r"HKD|%|процентов|процента|процент|месяцев|месяца|месяц|мес|months?|дн\.|дней|дня|день|days?"
QUANTITY = re.compile(
    rf"(?<![\w.,+\-−])(?P<number>{RAW_NUMBER}){SPACE}*(?P<unit>{UNIT})(?!\w)",
    re.IGNORECASE,
)
PREFIX_CASH = re.compile(
    rf"(?<!\w)(?P<unit>HKD){SPACE}+(?P<number>{RAW_NUMBER})(?!\w|[.,][0-9])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TextIssue:
    kind: Literal["digit", "number_word"]
    start: int
    end: int


class ChatTextError(GroundingError):
    def __init__(self, issues: tuple[TextIssue, ...]) -> None:
        super().__init__("UNCITED_NUMBER", ("text",))
        self.text_issues = issues[:8]
        self.text_issues_truncated = len(issues) > 8


@dataclass(frozen=True)
class NormalizedText:
    text: str
    fact_bindings: tuple[int, ...] = ()
    list_labels: int = 0


def _decimal_literal(raw: str) -> Decimal | None:
    """Parse only exact decimal notation; no arithmetic, rounding or scientific notation."""
    raw = raw.strip().replace("−", "-")
    formats = (
        (r"[+\-]?[0-9]+(?:[.,][0-9]{1,2})?", None, ","),
        (rf"[+\-]?[0-9]{{1,3}}(?:{SPACE}[0-9]{{3}})+(?:[.,][0-9]{{1,2}})?", SPACE, ","),
        (r"[+\-]?[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,2})?", ",", None),
        (r"[+\-]?[0-9]{1,3}(?:\.[0-9]{3})+(?:,[0-9]{1,2})?", r"\.", ","),
    )
    for pattern, separator, decimal in formats:
        if re.fullmatch(pattern, raw):
            if separator:
                raw = re.sub(separator, "", raw)
            if decimal:
                raw = raw.replace(decimal, ".")
            return Decimal(raw)
    return None


def _unit(raw: str) -> str:
    raw = raw.casefold()
    if raw == "hkd":
        return "HKD"
    if raw == "%" or raw.startswith("процент"):
        return "percent"
    if raw.startswith(("мес", "month")):
        return "months"
    return "days"


def normalize_text(text: str, facts: tuple[Fact, ...]) -> NormalizedText:
    """Bind exact, unit-matched literals only to already verified, unambiguous references."""
    if any(unicodedata.category(c) in {"Cc", "Cf"} and c not in "\n\t" for c in text):
        return NormalizedText(text)
    labels = list(LIST_LABEL.finditer(text))
    list_labels = 0
    if len(labels) >= 2 and [int(m["label"]) for m in labels] == list(range(1, len(labels) + 1)):
        # A bare currency/quantity line is not a prose list label.
        if not any(
            re.match(rf"(?:{UNIT})(?!\w)", text[m.end() :].lstrip("*_`"), re.IGNORECASE)
            for m in labels
        ):
            list_labels = len(labels)
            text = LIST_LABEL.sub(
                lambda m: (
                    m["prefix"] if m["prefix"].lstrip().startswith("#") else m["prefix"] + "- "
                ),
                text,
            )

    bindings: list[int] = []

    def bind(match: re.Match[str]) -> str:
        value, unit = _decimal_literal(match["number"]), _unit(match["unit"])
        if value is None:
            return match.group()
        indices = [
            index
            for index, fact in enumerate(facts)
            if fact.unit == unit and isinstance(fact.value, str) and Decimal(fact.value) == value
        ]
        if len(indices) != 1:
            return match.group()
        bindings.append(indices[0])
        return "{{fact:" + str(indices[0]) + "}}"

    text = QUANTITY.sub(bind, text)
    text = PREFIX_CASH.sub(bind, text)
    return NormalizedText(text, tuple(bindings), list_labels)


def number_issues(text: str) -> tuple[TextIssue, ...]:
    # Keep offsets relative to the original candidate; never put its tokens in diagnostics.
    remainder = PLACEHOLDER.sub(lambda m: " " * len(m.group()), text)
    remainder = re.sub(
        r"\{\{fact:f(?:0|[1-9][0-9]*)\}\}", lambda m: " " * len(m.group()), remainder
    )
    remainder = IDIOM.sub(lambda m: " " * len(m.group()), remainder)
    issues = [
        TextIssue("number_word", m.start(), m.end())
        for m in NUMBER_WORD.finditer(remainder)
        if _quantitative_word(m, remainder)
    ]
    issues.extend(
        TextIssue("digit", m.start(), m.end())
        for m in re.finditer(r"\S+", remainder)
        if any(c.isnumeric() for c in m.group())
    )
    return tuple(sorted(issues, key=lambda issue: issue.start))


def _placeholder_usage(text: str, reference_count: int) -> set[int]:
    if any(unicodedata.category(c) in {"Cc", "Cf"} and c not in "\n\t" for c in text):
        raise GroundingError("UNSAFE_CHAT_TEXT", ("text",))
    matches = list(PLACEHOLDER.finditer(text))
    used = {int(match.group(1)) for match in matches}
    if any(i >= reference_count for i in used):
        raise GroundingError("INVALID_FACT_PLACEHOLDER", ("text",))
    remainder = PLACEHOLDER.sub("", text)
    if "{{" in remainder or "}}" in remainder:
        raise GroundingError("INVALID_FACT_PLACEHOLDER", ("text",))
    return used


def validate_placeholders(text: str, reference_count: int) -> None:
    used = _placeholder_usage(text, reference_count)
    if used != set(range(reference_count)):
        raise GroundingError("UNUSED_FACT_REFERENCE", ("references",))


def validate_text(text: str, reference_count: int) -> None:
    used = _placeholder_usage(text, reference_count)
    if issues := number_issues(text):
        raise ChatTextError(issues)
    if used != set(range(reference_count)):
        raise GroundingError("UNUSED_FACT_REFERENCE", ("references",))

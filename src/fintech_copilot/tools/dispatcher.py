from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, cast

from pydantic import ValidationError

from fintech_copilot.domain.finance import anomalies, categorize, liquidity
from fintech_copilot.domain.models import Dataset, Scope
from fintech_copilot.domain.results import (
    AnomalyResult,
    CategorizationResult,
    ErrorResult,
    LiquidityResult,
    Success,
    TaxResult,
    ToolError,
    ToolName,
    ToolResult,
)
from fintech_copilot.domain.tax import RULESET_ID, draft_tax

from .catalogue import ToolData, catalogue

REGISTRY: MappingProxyType[str, Callable[[Dataset, Scope], ToolData]] = MappingProxyType(
    {
        "CalculateLiquidityRunway": liquidity,
        "CategorizeOpex": categorize,
        "DetectCashFlowAnomalies": lambda dataset, scope: anomalies(dataset, scope)[0],
        "DraftHKProfitsTax": draft_tax,
    }
)
TOOLS = tuple(REGISTRY)


@dataclass(frozen=True)
class RejectedCall:
    """Protocol error for a name outside the result schema's closed tool enum."""

    code: Literal["INVALID_ARGUMENTS"] = "INVALID_ARGUMENTS"
    message: str = "Tool name is not in the whitelist"

    def model_dump_json(self) -> str:
        return json.dumps(
            {
                "status": "error",
                "error": {"code": self.code, "message": self.message, "retryable": False},
            }
        )


class Dispatcher:
    def __init__(self, dataset: Dataset) -> None:
        self.dataset = dataset
        self.cache: dict[tuple[str, Scope], Success] = {}

    def error(self, name: ToolName, code: str, message: str) -> ErrorResult:
        scope = self.dataset.scope
        digest = hashlib.sha256(f"{name}:{code}:{message}".encode()).hexdigest()[:16]
        return ErrorResult.model_validate(
            {
                "schema_version": "1.0",
                "tool_result_id": f"error:{digest}",
                **scope.model_dump(),
                "tool_name": name,
                "status": "error",
                "error": ToolError.model_validate(
                    {"code": code, "message": message, "retryable": False}
                ),
            }
        )

    def dispatch(self, name: str, arguments: str) -> ToolResult | RejectedCall:
        if name not in REGISTRY:
            return RejectedCall()
        tool_name = cast(ToolName, name)
        if len(arguments.encode()) > 8192:
            return self.error(tool_name, "INVALID_ARGUMENTS", "Arguments exceed 8 KiB")
        try:
            raw = json.loads(arguments)
            if not isinstance(raw, dict):
                raise ValueError("Arguments must be an object")
            scope = Scope.model_validate(raw)
        except (ValueError, ValidationError):
            return self.error(tool_name, "INVALID_ARGUMENTS", "Invalid JSON or scope arguments")
        try:
            self.dataset.select(scope)
        except ValueError as exc:
            return self.error(tool_name, str(exc), "Arguments do not match the server snapshot")
        key = (name, scope)
        if key in self.cache:
            return self.cache[key]
        try:
            warnings: tuple[str, ...] = ()
            data: ToolData
            if name == "DetectCashFlowAnomalies":
                data, warnings = anomalies(self.dataset, scope)
            else:
                data = REGISTRY[name](self.dataset, scope)
            canonical = json.dumps(
                {
                    "scope": scope.model_dump(mode="json"),
                    "tool": name,
                    "dataset": self.dataset.model_dump(mode="json"),
                    "ruleset": RULESET_ID,
                    "algorithm": "finance-v1",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            result_id = f"result:{hashlib.sha256(canonical.encode()).hexdigest()[:24]}"
            payload = {
                "schema_version": "1.0",
                "tool_result_id": result_id,
                **scope.model_dump(),
                "tool_name": name,
                "status": "ok",
                "data": data,
                "facts": catalogue(self.dataset, scope, result_id, data),
                "warnings": warnings,
            }
            result: Success
            if name == "CalculateLiquidityRunway":
                result = LiquidityResult.model_validate(payload)
            elif name == "CategorizeOpex":
                result = CategorizationResult.model_validate(payload)
            elif name == "DetectCashFlowAnomalies":
                result = AnomalyResult.model_validate(payload)
            else:
                result = TaxResult.model_validate(payload)
            self.cache[key] = result
            return result
        except Exception:
            return self.error(tool_name, "INTERNAL_TOOL_ERROR", "Deterministic tool failed")

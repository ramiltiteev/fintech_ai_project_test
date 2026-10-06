from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import TypeAdapter

from fintech_copilot.domain.models import Dataset, Scope
from fintech_copilot.domain.results import (
    AnomalyData,
    CategorizationData,
    Fact,
    LiquidityData,
    TaxData,
    ToolResult,
)
from fintech_copilot.orchestration.models import Report, Synthesis

ROOT = Path(__file__).resolve().parents[1]


def test_packaged_resources_match_source():
    for name, source in {
        "scenarios.json": "fixtures/scenarios.json",
        "tools.json": "contracts/tools.json",
        "synthesis.schema.json": "contracts/synthesis.schema.json",
        "agent-system.md": "prompts/agent-system.md",
    }.items():
        assert (
            files("fintech_copilot.resources").joinpath(name).read_bytes()
            == (ROOT / source).read_bytes()
        )


def test_runtime_export_contract_fields_and_examples(golden):
    models = {
        "dataset": Dataset,
        "tool-arguments": Scope,
        "liquidity-data": LiquidityData,
        "categorization-data": CategorizationData,
        "anomaly-data": AnomalyData,
        "tax-data": TaxData,
        "fact": Fact,
        "synthesis": Synthesis,
        "report": Report,
    }
    for name, model in models.items():
        runtime = model.model_json_schema(mode="serialization")
        normative = json.loads((ROOT / "contracts" / f"{name}.schema.json").read_text())
        Draft202012Validator.check_schema(runtime)
        assert set(runtime["properties"]) == set(normative["properties"])
        assert set(runtime["required"]) == set(normative["required"])
    for dataset in golden.values():
        Draft202012Validator(
            Dataset.model_json_schema(mode="serialization"), format_checker=FormatChecker()
        ).validate(dataset.model_dump(mode="json"))
    sample = json.loads((ROOT / "fixtures/tool-result.example.json").read_text())
    result = TypeAdapter(ToolResult).validate_python(sample)
    assert result.model_dump(mode="json") == sample
    synthesis = json.loads((ROOT / "fixtures/synthesis.example.json").read_text())
    assert Synthesis.model_validate(synthesis).model_dump(mode="json") == synthesis

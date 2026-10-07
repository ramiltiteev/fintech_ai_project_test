"""Export runtime schemas for review; never rewrite normative contracts or fixtures."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import BaseModel, TypeAdapter

from fintech_copilot.domain.models import Dataset, Scope
from fintech_copilot.domain.results import (
    AnomalyData,
    CategorizationData,
    Fact,
    LiquidityData,
    TaxData,
    ToolResult,
)
from fintech_copilot.orchestration.chat import ChatReply
from fintech_copilot.orchestration.chat_reply import ComposedReply, ModelReply
from fintech_copilot.orchestration.conversation import ConversationReply
from fintech_copilot.orchestration.models import Report, Synthesis


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/schemas"))
    args = parser.parse_args()
    models: dict[str, type[BaseModel]] = {
        "dataset": Dataset,
        "tool-arguments": Scope,
        "liquidity-data": LiquidityData,
        "categorization-data": CategorizationData,
        "anomaly-data": AnomalyData,
        "tax-data": TaxData,
        "fact": Fact,
        "synthesis": Synthesis,
        "report": Report,
        "chat-reply": ChatReply,
        "conversation-reply": ConversationReply,
        "model-reply": ModelReply,
        "composed-reply": ComposedReply,
    }
    schemas = {
        name: model.model_json_schema(mode="serialization") for name, model in models.items()
    }
    schemas["tool-result"] = TypeAdapter(ToolResult).json_schema(mode="serialization")
    args.output.mkdir(parents=True, exist_ok=True)
    for name, schema in schemas.items():
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        (args.output / f"{name}.schema.json").write_text(json.dumps(schema, indent=2) + "\n")
    print(f"Exported {len(schemas)} runtime schemas to {args.output}")


if __name__ == "__main__":
    main()

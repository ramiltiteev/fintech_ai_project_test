"""Explicitly selected, billable API smoke; excluded from the default test suite."""

from __future__ import annotations

import asyncio
import json
from importlib.resources import files

import pytest

from fintech_copilot.domain.generator import generate
from fintech_copilot.orchestration.agent import run_agent
from fintech_copilot.orchestration.providers import ConfigurationError, LiveProvider
from fintech_copilot.tools.dispatcher import TOOLS


@pytest.mark.live
def test_live_function_calling_smoke(tmp_path):
    try:
        provider = LiveProvider()
    except ConfigurationError as exc:
        pytest.skip(str(exc))
    config = json.loads(files("fintech_copilot.resources").joinpath("scenarios.json").read_text())
    dataset = generate(config, "cloudscale-ai", 42)
    output = asyncio.run(run_agent(dataset, "Check runway and cash anomalies", provider))
    (tmp_path / "report.json").write_text(output.report.model_dump_json(indent=2))
    (tmp_path / "trace.jsonl").write_text("\n".join(json.dumps(e) for e in output.trace) + "\n")
    assert output.report.provider_mode == "live"
    assert output.report.status == "complete", output.report.partial_reasons
    assert {r.tool_name for r in output.report.tool_results if r.status == "ok"} == set(TOOLS)

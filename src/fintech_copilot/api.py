"""Local HTTP adapter for the existing grounded analysis pipeline."""

from __future__ import annotations

import json
import logging
from importlib.resources import files
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Response
from pydantic import ConfigDict, Field, StringConstraints

from fintech_copilot.domain.generator import generate
from fintech_copilot.domain.models import Company, FrozenModel
from fintech_copilot.orchestration.agent import run_agent
from fintech_copilot.orchestration.models import Report
from fintech_copilot.orchestration.providers import ConfigurationError, LiveProvider, MockProvider
from fintech_copilot.render import markdown

CompanyId = Literal["apex-logistics", "cloudscale-ai", "harbour-trade"]
Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)]
logger = logging.getLogger("uvicorn.error")


class ReportRequest(FrozenModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"company_id": "cloudscale-ai", "seed": 42, "provider": "mock"}]
        }
    )
    company_id: CompanyId
    seed: Annotated[int, Field(strict=True, ge=0)] = 42
    provider: Literal["mock", "live"] = "mock"
    question: Question | None = None


class Health(FrozenModel):
    status: Literal["ok"] = "ok"


app = FastAPI(
    title="FinTech & AI Co-Pilot",
    version="0.1.0",
    description=(
        "Grounded reports from synthetic company transactions. "
        "Mock is the default; live requests explicitly use the server's configured LLM Gateway."
    ),
)


def scenario_config() -> dict[str, Any]:
    config: dict[str, Any] = json.loads(
        files("fintech_copilot.resources").joinpath("scenarios.json").read_text()
    )
    return config


def log_event(event: dict[str, Any]) -> None:
    logger.info("agent_event %s", json.dumps(event, ensure_ascii=False))


@app.get("/health", summary="Check process health")
async def health() -> Health:
    return Health()


@app.get("/companies", summary="List available synthetic company profiles")
async def companies() -> tuple[Company, ...]:
    return tuple(
        Company.model_validate(profile["company"]) for profile in scenario_config()["profiles"]
    )


@app.post(
    "/reports",
    response_model=Report,
    summary="Generate a grounded company report",
    description=(
        "Runs the same tools and agent as the CLI. Returns a complete or partial report; "
        "inspect status and partial_reasons. Live runs may take up to 300 seconds."
    ),
    responses={
        200: {
            "content": {"text/markdown": {"schema": {"type": "string"}}},
            "headers": {
                "X-Run-ID": {"schema": {"type": "string"}},
                "X-Report-Status": {"schema": {"enum": ["complete", "partial"]}},
            },
        },
        503: {"description": "Live provider is not configured on the server"},
    },
)
async def create_report(
    body: ReportRequest,
    response: Response,
    format: Literal["json", "markdown"] = "json",
) -> Report | Response:
    try:
        provider = LiveProvider() if body.provider == "live" else MockProvider()
    except ConfigurationError:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "LIVE_NOT_CONFIGURED",
                "message": "Live provider is not configured on the server.",
            },
        ) from None
    config = scenario_config()
    profile = next(p for p in config["profiles"] if p["company"]["company_id"] == body.company_id)
    dataset = generate(config, body.company_id, body.seed)
    run = await run_agent(
        dataset, body.question or profile["business_question"], provider, trace_sink=log_event
    )
    headers = {"X-Run-ID": run.report.run_id, "X-Report-Status": run.report.status}
    if format == "markdown":
        return Response(markdown(run.report), media_type="text/markdown", headers=headers)
    response.headers.update(headers)
    return run.report

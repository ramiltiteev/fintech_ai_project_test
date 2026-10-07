"""HTTP transport exercises the actual generator, dispatcher, agent and renderer."""

from __future__ import annotations

import asyncio
import builtins
import json
import logging
import sys
from importlib.resources import files
from types import SimpleNamespace

import httpx
import pytest

from fintech_copilot import api
from fintech_copilot.domain.generator import generate
from fintech_copilot.orchestration.grounding import validate_synthesis
from fintech_copilot.orchestration.models import Report
from fintech_copilot.orchestration.providers import MockProvider, ProviderFailure
from fintech_copilot.render import markdown
from fintech_copilot.tools.dispatcher import TOOLS, Dispatcher


def request(method, path, **kwargs):
    async def send():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api.app), base_url="http://test"
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(send())


def config():
    return json.loads(files("fintech_copilot.resources").joinpath("scenarios.json").read_text())


@pytest.mark.parametrize("cid", ["apex-logistics", "cloudscale-ai", "harbour-trade"])
def test_http_real_mock_report_with_default_mode_and_trace(cid, validator, monkeypatch, caplog):
    original_import = builtins.__import__

    def no_sdk(name, *args, **kwargs):
        if name == "litellm" or name.startswith("litellm."):
            raise AssertionError("Mock HTTP report must not import the LLM SDK")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_sdk)
    monkeypatch.setenv("LLM_MODEL", "invalid-configuration-ignored-in-mock")
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    response = request("POST", "/reports", json={"company_id": cid})
    assert response.status_code == 200
    raw = response.json()
    validator("report.schema.json").validate(raw)
    report = Report.model_validate(raw)
    assert report.status == "complete" and report.provider_mode == "mock"
    assert response.headers["X-Run-ID"] == report.run_id
    assert response.headers["X-Report-Status"] == "complete"
    dataset = generate(config(), cid, 42)
    validate_synthesis(report.synthesis, report.tool_results, dataset)
    dispatcher = Dispatcher(dataset)
    assert len(report.tool_results) == 4
    for result in report.tool_results:
        assert result == dispatcher.dispatch(result.tool_name, dataset.scope.model_dump_json())
    assert isinstance(raw["tool_results"][0]["data"]["closing_cash"], str)
    events = [
        json.loads(record.message.removeprefix("agent_event "))
        for record in caplog.records
        if record.message.startswith("agent_event ")
    ]
    assert all(event["run_id"] == report.run_id for event in events)
    assert {e["tool"] for e in events if e["event"] == "tool_call"} == set(TOOLS)
    assert events[-1]["event"] == "run_finished" and events[-1]["status"] == "complete"


def test_http_markdown_matches_renderer_and_json_repeat(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    body = {"company_id": "cloudscale-ai", "seed": 7, "question": "  Check cash  "}
    json_response = request("POST", "/reports", json=body)
    md_response = request("POST", "/reports?format=markdown", json=body)
    report = Report.model_validate(json_response.json())
    assert md_response.status_code == 200
    assert md_response.headers["content-type"].startswith("text/markdown")
    assert md_response.headers["X-Report-Status"] == "complete"
    assert md_response.headers["X-Run-ID"] != report.run_id
    assert md_response.text == markdown(report)
    repeated = Report.model_validate(request("POST", "/reports", json=body).json())
    assert report.model_dump(exclude={"run_id", "generated_at"}) == repeated.model_dump(
        exclude={"run_id", "generated_at"}
    )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "changes,path",
    [
        ({"company_id": "other"}, "/reports"),
        ({"seed": -1}, "/reports"),
        ({"seed": True}, "/reports"),
        ({"seed": 42.0}, "/reports"),
        ({"seed": "42"}, "/reports"),
        ({"provider": "automatic"}, "/reports"),
        ({"question": "   "}, "/reports"),
        ({"question": "q" * 4097}, "/reports"),
        ({"opening_cash": "9999999.00"}, "/reports"),
        ({"api_key": "synthetic-request-key"}, "/reports"),
        ({"output": "/tmp/not-client-controlled"}, "/reports"),
        ({}, "/reports?format=pdf"),
    ],
)
def test_invalid_requests_do_not_start_agent(changes, path, monkeypatch):
    async def fail_if_called(*args, **kwargs):
        raise AssertionError("Invalid request must be rejected before any agent work")

    monkeypatch.setattr(api, "run_agent", fail_if_called)
    response = request("POST", path, json={"company_id": "cloudscale-ai", **changes})
    assert response.status_code == 422


def test_health_companies_and_swagger_do_not_need_provider(monkeypatch):
    def fail_provider():
        raise AssertionError("Health, companies and docs must not create an LLM provider")

    monkeypatch.setattr(api, "LiveProvider", fail_provider)
    monkeypatch.setattr(api, "MockProvider", fail_provider)
    assert request("GET", "/health").json() == {"status": "ok"}
    companies = request("GET", "/companies").json()
    assert companies == [p["company"] for p in config()["profiles"]]
    docs = request("GET", "/docs")
    assert docs.status_code == 200 and "swagger-ui" in docs.text
    schema = request("GET", "/openapi.json").json()
    assert {"/health", "/companies", "/reports"} <= schema["paths"].keys()
    request_schema = schema["components"]["schemas"]["ReportRequest"]
    assert request_schema["properties"]["provider"]["default"] == "mock"
    assert request_schema["additionalProperties"] is False
    assert request_schema["required"] == ["company_id"]
    media = schema["paths"]["/reports"]["post"]["responses"]["200"]["content"]
    assert {"application/json", "text/markdown"} <= media.keys()


def test_live_configuration_failure_safe_and_no_fallback(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)

    def fail_mock():
        raise AssertionError("Live failure must not fall back to mock")

    monkeypatch.setattr(api, "MockProvider", fail_mock)
    response = request(
        "POST",
        "/reports",
        json={"company_id": "cloudscale-ai", "provider": "live", "question": "  Check runway  "},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "LIVE_NOT_CONFIGURED"
    assert "LLM_MODEL" not in response.text


def test_http_live_sdk_transport_and_json_mode(monkeypatch, caplog):
    """Synthetic SDK transport only; it does not contact a real Gateway."""
    secret = "synthetic-http-gateway-key"
    monkeypatch.setenv("LLM_MODEL", "litellm_proxy/synthetic-model")
    monkeypatch.setenv("LITELLM_PROXY_API_BASE", "https://gateway.test/v1")
    monkeypatch.setenv("LITELLM_PROXY_API_KEY", secret)
    mock = MockProvider()
    requests = []

    async def acompletion(**kwargs):
        requests.append(kwargs)
        turn = await mock.complete(kwargs["messages"], kwargs.get("tools", []), kwargs["timeout"])
        return {"choices": [{"message": turn.message()}]}

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=acompletion))
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    response = request(
        "POST",
        "/reports",
        json={"company_id": "cloudscale-ai", "provider": "live", "question": "  Check runway  "},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "complete" and response.json()["provider_mode"] == "live"
    assert all(
        r["api_base"] == "https://gateway.test/v1" and r["api_key"] == secret for r in requests
    )
    assert requests[-1]["response_format"] == {"type": "json_object"}
    assert json.loads(requests[0]["messages"][1]["content"])["question"] == "Check runway"
    assert secret not in response.text + caplog.text


@pytest.mark.parametrize("format", ["json", "markdown"])
def test_provider_failure_returns_visible_partial_report(monkeypatch, validator, format):
    class Failing:
        mode = "live"
        model = "litellm_proxy/synthetic-model"

        async def complete(self, *args, **kwargs):
            raise ProviderFailure("AUTH_ERROR")

    monkeypatch.setattr(api, "LiveProvider", Failing)
    response = request(
        "POST",
        f"/reports?format={format}",
        json={"company_id": "cloudscale-ai", "provider": "live"},
    )
    assert response.status_code == 200 and response.headers["X-Report-Status"] == "partial"
    if format == "json":
        validator("report.schema.json").validate(response.json())
        assert response.json()["synthesis"] is None
        assert "AUTH_ERROR" in response.json()["partial_reasons"]
    else:
        assert "partial" in response.text and "AUTH_ERROR" in response.text

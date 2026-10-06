from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from fintech_copilot.domain.models import Dataset

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def golden():
    return {
        cid: Dataset.model_validate_json((ROOT / "fixtures/golden" / f"{cid}.json").read_text())
        for cid in ("apex-logistics", "cloudscale-ai", "harbour-trade")
    }


@pytest.fixture
def expected():
    return json.loads((ROOT / "fixtures/golden/expected.json").read_text())


@pytest.fixture
def scenarios():
    return json.loads((ROOT / "fixtures/scenarios.json").read_text())


@pytest.fixture
def validator():
    schemas = {
        path.name: json.loads(path.read_text())
        for path in (ROOT / "contracts").glob("*.schema.json")
    }
    registry = Registry().with_resources(
        (name, Resource.from_contents(schema)) for name, schema in schemas.items()
    )
    return lambda name: Draft202012Validator(
        schemas[name], registry=registry, format_checker=FormatChecker()
    )

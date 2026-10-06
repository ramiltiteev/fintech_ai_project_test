#!/usr/bin/env python3
"""Check preparation artifacts; this does not execute the future application."""

from __future__ import annotations

import argparse
import json
import re
import tomllib
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from statistics import median
from typing import Any
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
CENT = Decimal("0.01")
MONEY = re.compile(r"^-?(0|[1-9][0-9]*)\.[0-9]{2}$")
TOOLS = {
    "CalculateLiquidityRunway",
    "CategorizeOpex",
    "DetectCashFlowAnomalies",
    "DraftHKProfitsTax",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def money(value: str) -> Decimal:
    require(isinstance(value, str) and bool(MONEY.fullmatch(value)), f"Bad money: {value}")
    return Decimal(value)


def display(value: Decimal) -> str:
    return str(value.quantize(CENT, rounding=ROUND_HALF_UP))


def walk(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def check_documents() -> int:
    count = 0
    paths = [ROOT / "README.md", ROOT / "AGENTS.md"]
    paths.extend(
        path
        for base in ("docs", "contracts", "fixtures", "prompts", "src")
        for path in (ROOT / base).rglob("*.md")
        if not path.is_relative_to(ROOT / "docs/source")
    )
    for path in paths:
        for target in re.findall(r"(?<!!)\[[^\]]*\]\(([^)]+)\)", path.read_text()):
            if urlparse(target).scheme or target.startswith("#"):
                continue
            local = unquote(target.split("#", 1)[0])
            require((path.parent / local).exists(), f"Broken link: {path.name} -> {target}")
            count += 1
    source_path = ROOT / "docs/source/assignment.txt"
    if source_path.exists():
        source = source_path.read_text()
        for name in TOOLS - {"DraftHKProfitsTax"}:
            require(name in source, f"Missing source requirement: {name}")
        for name in ("Apex Logistics", "CloudScale AI", "Harbour Trade", "3–5 часов"):
            require(name in source, f"Missing source text: {name}")
    tomllib.loads((ROOT / "pyproject.toml").read_text())
    return count


def check_contracts(documents: dict[Path, Any]) -> int:
    schemas = [p for p in documents if p.name.endswith(".schema.json")]
    for path in schemas:
        schema = documents[path]
        require(schema["$id"] == path.name, f"Schema ID mismatch: {path}")
        require(schema["$schema"].endswith("2020-12/schema"), f"Wrong schema dialect: {path}")
        for node in walk(schema):
            if "$ref" in node:
                target = node["$ref"].split("#", 1)[0]
                require((path.parent / target).exists(), f"Unresolved schema ref: {target}")
            if "pattern" in node:
                re.compile(node["pattern"])
            if node.get("type") == "object":
                props = node.get("properties", {})
                require(set(node.get("required", [])) <= props.keys(), f"Bad required: {path}")
                require(node.get("additionalProperties") is False, f"Open object: {path}")
    definitions = load(ROOT / "contracts/tools.json")
    require(len(definitions) == 4, "Expected four tools")
    require({t["function"]["name"] for t in definitions} == TOOLS, "Tool names differ")
    arguments = load(ROOT / "contracts/tool-arguments.schema.json")
    for tool in definitions:
        actual = tool["function"]["parameters"]
        require(
            actual
            == {k: v for k, v in arguments.items() if not k.startswith("$") and k != "title"},
            "Inline argument schema drift",
        )
    return len(schemas)


def check_golden() -> int:
    expected = load(ROOT / "fixtures/golden/expected.json")
    total = 0
    for company_id, oracle in expected.items():
        data = load(ROOT / f"fixtures/golden/{company_id}.json")
        txs = data["transactions"]
        require(data["company"]["company_id"] == company_id, "Company mismatch")
        require(len(txs) == oracle["transaction_count"], f"Count mismatch: {company_id}")
        require(len({t["transaction_id"] for t in txs}) == len(txs), "Duplicate IDs")
        require(
            txs == sorted(txs, key=lambda t: (t["posted_on"], t["transaction_id"])),
            "Unstable fixture order",
        )
        start, end = (
            date.fromisoformat(data["period_start"]),
            date.fromisoformat(data["period_end"]),
        )
        opening = money(data["opening_balance"])
        signed_by_day: dict[date, Decimal] = {}
        for tx in txs:
            day = date.fromisoformat(tx["posted_on"])
            amount = money(tx["amount"])
            require(start <= day < end and amount > 0, "Bad date/amount")
            require(
                tx["currency"] == "HKD" and tx["evidence"]["synthetic"] is True,
                "Fixture must be synthetic HKD",
            )
            signed = amount if tx["direction"] == "credit" else -amount
            signed_by_day[day] = signed_by_day.get(day, Decimal(0)) + signed
            if tx["invoice_issued_on"] is not None:
                lag = (day - date.fromisoformat(tx["invoice_issued_on"])).days
                require(lag == tx["payment_terms_days"], "Golden paid invoice lag mismatch")
        closing = opening + sum(signed_by_day.values(), Decimal(0))
        require(display(closing) == oracle["closing_cash"], f"Closing mismatch: {company_id}")
        operating = [t for t in txs if t["movement_type"] == "operating"]
        inflows = sum(
            (money(t["amount"]) for t in operating if t["direction"] == "credit"), Decimal(0)
        )
        outflows = sum(
            (money(t["amount"]) for t in operating if t["direction"] == "debit"), Decimal(0)
        )
        require(display(inflows) == oracle["operating_inflows"], "Operating inflows mismatch")
        require(display(outflows) == oracle["operating_outflows"], "Operating outflows mismatch")
        months = Decimal((end.year - start.year) * 12 + end.month - start.month)
        burn = (outflows - inflows) / months
        require(display(burn) == oracle["average_net_burn"], "Net burn mismatch")
        runway = display(closing / burn) if closing > 0 and burn > 0 else None
        require(runway == oracle["runway_months"], "Golden runway mismatch")
        require(
            display(closing / (outflows / months)) == oracle["gross_coverage_months"],
            "Gross coverage mismatch",
        )
        last = [t for t in operating if t["posted_on"][:7] == "2026-03"]
        last_burn = sum(
            (money(t["amount"]) * (1 if t["direction"] == "debit" else -1) for t in last),
            Decimal(0),
        )
        stress = display(closing / last_burn) if last_burn > 0 else None
        require(stress == oracle["last_month_stress_runway_months"], "Stress runway mismatch")
        # Independent EOD arithmetic validates the hand-authored gap dates.
        cash, minimum, min_day, gap_start = opening, opening, start, None
        gaps = []
        day = start
        while day < end:
            cash += signed_by_day.get(day, Decimal(0))
            if cash < minimum:
                minimum, min_day = cash, day
            if cash < 0 and gap_start is None:
                gap_start = day
            if cash >= 0 and gap_start is not None:
                gaps.append(
                    {
                        "start": gap_start.isoformat(),
                        "end": day.isoformat(),
                        "open_at_period_end": False,
                    }
                )
                gap_start = None
            day += timedelta(days=1)
        if gap_start is not None:
            gaps.append(
                {"start": gap_start.isoformat(), "end": end.isoformat(), "open_at_period_end": True}
            )
        require(display(minimum) == oracle["minimum_cash"], "Minimum cash mismatch")
        require(min_day.isoformat() == oracle["minimum_cash_date"], "Minimum date mismatch")
        require(gaps == oracle["cash_gap_intervals"], "Gap intervals mismatch")
        debits = [t for t in txs if t["direction"] == "debit"]
        debit_total = sum((money(t["amount"]) for t in debits), Decimal(0))
        tax_total = sum((money(v) for v in oracle["tax_decision_totals"].values()), Decimal(0))
        require(tax_total == debit_total, "Tax oracle reconciliation mismatch")
        if company_id == "cloudscale-ai":
            baseline = [
                money(t["amount"])
                for t in txs
                if t["category"] == "rd_contractors" and t["posted_on"] < "2026-03-01"
            ]
            require(len(baseline) == oracle["outlier_baseline_count"], "Baseline count mismatch")
            require(
                display(median(baseline)) == oracle["outlier_baseline_median"], "Median mismatch"
            )
            require(
                display(3 * median(baseline)) == oracle["outlier_threshold"], "Threshold mismatch"
            )
        if company_id == "harbour-trade":
            unknown = [
                t
                for t in debits
                if t["category"] == "unknown" and t["counterparty_country"] != "HK"
            ]
            require(
                [t["transaction_id"] for t in unknown] == oracle["unclassified_cross_border_ids"],
                "Unknown cross-border IDs mismatch",
            )
            require(
                display(sum((money(t["amount"]) for t in unknown), Decimal(0)))
                == oracle["unclassified_cross_border_total"],
                "Unknown total mismatch",
            )
        total += len(txs)
    return total


def check_examples() -> None:
    result = load(ROOT / "fixtures/tool-result.example.json")
    synthesis = load(ROOT / "fixtures/synthesis.example.json")
    data = load(ROOT / "fixtures/golden/cloudscale-ai.json")
    expected = load(ROOT / "fixtures/golden/expected.json")["cloudscale-ai"]
    ids = {t["transaction_id"] for t in data["transactions"]}
    facts = {f["fact_id"]: f for f in result["facts"]}
    require(result["snapshot_id"] == data["snapshot_id"], "Example snapshot mismatch")
    for fact in facts.values():
        require(set(fact["transaction_ids"]) <= ids, "Example cites nonexistent transaction")
        require(fact["tool_result_id"] == result["tool_result_id"], "Example result ID mismatch")
        require(
            fact["value"] == result["data"][fact["metric"]] == expected[fact["metric"]],
            "Example fact value drift",
        )
    for insight in synthesis["insights"]:
        for evidence in insight["evidence"]:
            require(
                evidence["tool_result_id"] == result["tool_result_id"], "Fabricated example result"
            )
            require(evidence["fact_id"] in facts, "Fabricated example fact")
    scenario = load(ROOT / "fixtures/scenarios.json")
    require(len(scenario["profiles"]) == 3, "Expected three scenario profiles")
    require(
        [p["transaction_count"] for p in scenario["profiles"]] == [90, 120, 150],
        "Cohort targets differ",
    )
    require(
        scenario["period_start"] == "2026-07-01" and scenario["period_end"] == "2026-10-01",
        "Cohort period differs",
    )


def check_json_schemas(documents: dict[Path, Any]) -> None:
    try:
        # Optional preparation dependency; runtime does not require typing stubs.
        from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
        from referencing import Registry, Resource
    except ImportError as exc:
        raise ValueError(
            "--schemas requires jsonschema; install requirements-preparation.txt"
        ) from exc
    schemas = {p.name: value for p, value in documents.items() if p.name.endswith(".schema.json")}
    registry = Registry().with_resources(
        (name, Resource.from_contents(value)) for name, value in schemas.items()
    )
    for schema in schemas.values():
        Draft202012Validator.check_schema(schema)
    mappings = {
        "tool-result.example.json": "tool-result.schema.json",
        "synthesis.example.json": "synthesis.schema.json",
    }
    mappings.update(
        {
            f"golden/{cid}.json": "dataset.schema.json"
            for cid in ("apex-logistics", "cloudscale-ai", "harbour-trade")
        }
    )
    for fixture, schema_name in mappings.items():
        validator = Draft202012Validator(
            schemas[schema_name], registry=registry, format_checker=FormatChecker()
        )
        validator.validate(load(ROOT / "fixtures" / fixture))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schemas", action="store_true", help="Full JSON Schema validation (optional dependency)"
    )
    args = parser.parse_args()
    try:
        documents = {
            path: load(path)
            for base in ("contracts", "fixtures")
            for path in (ROOT / base).rglob("*.json")
        }
        links = check_documents()
        schemas = check_contracts(documents)
        transactions = check_golden()
        check_examples()
        if args.schemas:
            check_json_schemas(documents)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(f"FAIL: {exc}")
        return 1
    print(
        f"OK: {len(documents)} JSON files, {schemas} schema structures/refs, {links} local links,"
    )
    print(
        f"3 golden datasets / {transactions} transactions, arithmetic and example evidence checked."
    )
    print(
        "Full JSON Schema validation: "
        + ("passed" if args.schemas else "not requested (--schemas).")
    )
    print("This check does not execute application tests or LLM runs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

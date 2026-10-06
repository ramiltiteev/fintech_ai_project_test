from __future__ import annotations

import json
import os
import subprocess
import sys

from fintech_copilot.cli import main


def canonical(report):
    return {key: value for key, value in report.items() if key not in {"run_id", "generated_at"}}


def test_all_offline_reproducible_cli(tmp_path, validator):
    outputs = [tmp_path / "first", tmp_path / "second"]
    for output in outputs:
        assert main(["demo", "--company", "all", "--seed", "42", "--output", str(output)]) == 0
        summary = json.loads((output / "summary.json").read_text())
        assert len(summary) == 3 and all(item["status"] == "complete" for item in summary)
        for item in summary:
            directory = output / item["company_id"]
            assert {p.name for p in directory.iterdir()} == {
                "dataset.json",
                "report.json",
                "report.md",
                "trace.jsonl",
            }
            report = json.loads((directory / "report.json").read_text())
            validator("report.schema.json").validate(report)
            dataset = json.loads((directory / "dataset.json").read_text())
            validator("dataset.schema.json").validate(dataset)
    for directory in outputs[0].iterdir():
        if not directory.is_dir():
            continue
        other = outputs[1] / directory.name
        assert (directory / "dataset.json").read_bytes() == (other / "dataset.json").read_bytes()
        assert canonical(json.loads((directory / "report.json").read_text())) == canonical(
            json.loads((other / "report.json").read_text())
        )
        assert (directory / "report.md").read_bytes() == (other / "report.md").read_bytes()


def test_cli_configuration_before_writes(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    output = tmp_path / "uncreated"
    assert main(["demo", "--provider", "live", "--output", str(output)]) == 2
    assert not output.exists()
    assert main(["demo", "--seed", "-1", "--output", str(output)]) == 2
    assert not output.exists()


def test_module_entrypoint_in_process(tmp_path):
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "fintech_copilot",
            "demo",
            "--company",
            "cloudscale-ai",
            "--output",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert "cloudscale-ai: complete" in completed.stdout

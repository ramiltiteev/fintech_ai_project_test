.PHONY: prep-check check demo

prep-check:
	python3 scripts/check_preparation.py

check:
	python -m pytest
	python -m ruff check .
	python -m ruff format --check .
	python -m mypy src

demo:
	python -m fintech_copilot demo --company all --provider mock --seed 42 --output artifacts/demo

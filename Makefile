# Developer shortcuts (POSIX shells). Windows users: run the `uv run ...` commands directly.
.PHONY: install lint format typecheck arch test cov schemas check security

install:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy

arch:
	uv run lint-imports

test:
	uv run pytest

cov:
	uv run pytest --cov --cov-report=term-missing --cov-report=html

schemas:
	uv run ai-coding config schema -o src/ai_coding/schemas/ai-coding.schema.json

check: lint typecheck arch cov
	uv run ai-coding config validate .ai-coding.yml

security:
	uv export --frozen --no-emit-project --all-groups --format requirements-txt -o .ai-coding-req.txt
	uvx pip-audit==2.10.1 --strict --require-hashes -r .ai-coding-req.txt; rc=$$?; rm -f .ai-coding-req.txt; exit $$rc

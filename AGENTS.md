# AGENTS.md — agent operating rules for this repository

## Exact commands

```bash
# setup (Python 3.9+; 3.12 recommended)
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# run the CLI (from the repo root so local .env is found)
python -m firecrawl_skill <subcommand>
python -m firecrawl_skill --help

# offline unit tests (no network, no API key needed)
python -m pytest tests/ -v

# live integration tests (opt-in; consumes Firecrawl credits)
RUN_FIRECRAWL_INTEGRATION=1 python -m pytest tests/test_integration.py -v
```

## Invariants

- Standard library only in `src/` — no third-party runtime dependencies. Tests may use pytest.
- The CLI surface is a mirror of `tavily-skill` for `search` and `extract` (same flag names and defaults, same JSON envelope and file-output behavior). Do not change flag names or the envelope shape; downstream workflows depend on them. `usage` has no tavily-skill counterpart and its own `data` block — it reuses the `{command, input, data}` envelope, the `--stdout`/`--output` semantics, and the exit-code contract, and adds nothing to `search`/`extract`.
- Every command prints exactly one JSON object to stdout: the full payload with `--stdout`, otherwise a lightweight status object with the payload path and a `payload_schema` hint.
- Search results always carry a `raw_content` field (markdown) when content was requested, even though the upstream Firecrawl field is named `markdown`.
- Exit codes are a contract, identical for every subcommand: 0 ok / 2 usage error (bad arguments) / 10 auth / 11 quota-rate / 12 rejected-no-data / 13 network-server. Do not add new exit codes without updating the README, `skills/skill_firecrawl.md`, and the unit tests together.
- No implicit retries. Transient failures are the caller's decision.
- Never commit a real API key or an `op://` vault reference. `.env` is gitignored; examples use `FIRECRAWL_API_KEY=fc-REPLACE_ME`.
- This repository's default branch is `master`.

## Layout

- `src/firecrawl_skill/` — package (`cli.py` owns argparse, request building, transport, normalization)
- `tests/` — `test_unit.py` (offline), `test_integration.py` (live, opt-in)
- `fixtures/` — recorded and anonymized Firecrawl v2 responses for offline normalization tests (billing fixtures use synthetic credit figures and shifted dates, never the live account's numbers)
- `skills/skill_firecrawl.md` — public agent skill document
- `docs/` — prd / rfc / working notes

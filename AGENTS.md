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

# latency benchmark (opt-in; spends real Firecrawl credits)
RUN_FIRECRAWL_LATENCY=1 python benchmarks/latency.py --queries 4 --repeats 2
```

## Invariants

- Standard library only in `src/` — no third-party runtime dependencies. Tests may use pytest.
- The CLI surface is a mirror of `tavily-skill` for `search` and `extract` (same flag names and defaults, same JSON envelope and file-output behavior), with one exception: `search` and `extract` do not accept `--stdout`. Do not change flag names or the envelope shape; downstream workflows depend on them. `usage` has no tavily-skill counterpart and its own `data` block — it reuses the `{command, input, data}` envelope, still accepts `--stdout`/`--output`, and the exit-code contract, and adds nothing to `search`/`extract`.
- `scrape` has no tavily-skill counterpart: it captures one URL in exactly one of `--raw-html`, `--html`, or `--screenshot`. `--output PATH` is required, and `scrape` never writes `FIRECRAWL_CLI_OUTPUT_DIR` or the auto-named snapshot corpus — the only file it writes is the one named by `--output`. It still prints exactly one JSON object to stdout (a status object, never the captured bytes), rejects `--stdout` (exit 2) before any request, and uses the unchanged exit-code contract: 12 for an oversized payload (`--max-bytes`), 13 for a failed screenshot download or transport failure. `extract` gains only the additive `--links` and `--only-main-content`; all other `search`/`extract` behavior and flags stay as they are.
- Every command prints exactly one JSON object to stdout. Successful `search` and `extract` always write the full payload to a file and emit a lightweight status object with the payload path and a `payload_schema` hint; `--stdout` on those commands is a usage error (exit 2) before any request. Only `usage --stdout` prints a full payload. `search` batch mode (repeatable `--query`) prints exactly one batch status object (one entry per query) and writes one full-payload file per query; `--stdout`/`--output` are rejected there, and a partial failure still exits 0.
- `search` accepts either one positional `query` (single mode, unchanged) or one or more `--query` values (batch mode, executed in parallel with the stdlib `ThreadPoolExecutor`). Never both, never neither. Batch concurrency is `--concurrency` (default 4); `--serial` forces 1 and overrides it.
- Search results always carry a `raw_content` field (markdown) when content was requested, even though the upstream Firecrawl field is named `markdown`.
- Exit codes are a contract, identical for every subcommand: 0 ok / 2 usage error (bad arguments) / 10 auth / 11 quota-rate / 12 rejected-no-data / 13 network-server. Do not add new exit codes without updating the README, `skills/skill_firecrawl.md`, and the unit tests together.
- No implicit retries. Transient failures are the caller's decision.
- Never commit a real API key or an `op://` vault reference. `.env` is gitignored; examples use `FIRECRAWL_API_KEY=fc-REPLACE_ME`.
- This repository's default branch is `master`.

## Layout

- `src/firecrawl_skill/` — package (`cli.py` owns argparse, request building, transport, normalization for `search`, `extract`, `scrape`, and `usage`; the `scrape` surface is specified in `docs/rfc.md` D9)
- `tests/` — `test_unit.py` (offline), `test_integration.py` (live, opt-in)
- `fixtures/` — recorded and anonymized Firecrawl v2 responses for offline normalization tests (billing fixtures use synthetic credit figures and shifted dates, never the live account's numbers)
- `skills/skill_firecrawl.md` — public agent skill document
- `benchmarks/` — opt-in latency benchmark (`latency.py`, stdlib only) and its methodology (`README.md`)
- `docs/` — prd / rfc / working notes

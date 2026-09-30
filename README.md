# firecrawl-skill

Agent-facing web search and URL extraction CLI backed by the [Firecrawl v2 API](https://docs.firecrawl.dev/api-reference/endpoint/search).

The CLI is a drop-in mirror of [tavily-skill](https://github.com/grapeot/tavily-skill): the same subcommands (`search`, `extract`), the same flag names and defaults, the same JSON envelope, and the same file-output behavior. Downstream workflows that consume tavily-skill payloads work unchanged.

## Quickstart

```bash
git clone https://github.com/grapeot/firecrawl-skill.git
cd firecrawl-skill
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# configure your key (see .env.example)
cp .env.example .env
```

```bash
# basic search (writes full payload to a file, prints status JSON)
python -m firecrawl_skill search "latest AI news" --time-range week

# search with result count and time range
python -m firecrawl_skill search "openai releases" --max-results 10 --time-range month

# restrict to specific domains
python -m firecrawl_skill search "agent framework" \
  --include-domain github.com \
  --include-domain docs.anthropic.com

# print full JSON directly to stdout
python -m firecrawl_skill search "AI coding tools" --stdout

# URL content extraction
python -m firecrawl_skill extract https://example.com
python -m firecrawl_skill extract https://example.com --query "agent search"
```

## Configuration

| Variable | Description |
|---|---|
| `FIRECRAWL_API_KEY` | Firecrawl API key (`fc-...`). Takes priority. |
| `ONEPASSWORD_FIRECRAWL_REFERENCE` | Optional `op read`-compatible reference (e.g. `op://Vault/Item/field`). Used when `FIRECRAWL_API_KEY` is not set. Requires the 1Password CLI. |
| `FIRECRAWL_CLI_OUTPUT_DIR` | Directory for auto-named payload files. Default: `./tmp/firecrawl/` (relative to the working directory). Point this at one dedicated, persistent directory so every payload accumulates as a timestamped corpus. |

A local `.env` file in the working directory (or any parent directory) is loaded automatically.

## Commands

### `search`

Web search via `POST /v2/search`. With `--raw-content markdown` (default), every result includes full-page markdown, which costs 1 additional Firecrawl credit per page on top of 2 credits per 10 results.

### `extract`

URL content extraction via `POST /v2/scrape`, one request per URL (1–20 URLs per call). `--query` requests query-relevant highlights for each page.

Full flag reference: `python -m firecrawl_skill search --help` / `extract --help`, or see `skills/skill_firecrawl.md`.

## Output

The top-level structure is fixed:

```json
{
  "command": "search",
  "input": {},
  "data": {
    "results": [],
    "images": [],
    "credits_used": 8,
    "result_count": 6
  }
}
```

In default mode the full payload is written to an auto-named file and stdout carries a lightweight status object. Use `--stdout` for the full payload inline, or `--output PATH` for a named file.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 2 | Usage error (bad arguments) |
| 10 | Authentication failed (401/403) |
| 11 | Quota or rate limit (402/429) |
| 12 | Request rejected, no data (other 4xx) |
| 13 | Timeout or server error (408/5xx/network) |

## Differences from tavily-skill

Documented upstream gaps and how this CLI handles them: `docs/rfc.md` ("Capability gaps"). In short: search depth tiers are ignored with a warning, image descriptions and the `finance` topic are rejected, and domain include/exclude cannot be combined (Firecrawl constraint).

## Tests

```bash
python -m pytest tests/ -v                      # offline unit tests
RUN_FIRECRAWL_INTEGRATION=1 python -m pytest tests/test_integration.py -v   # live, consumes credits
```

## License

MIT — see [LICENSE](LICENSE).

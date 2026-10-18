# firecrawl-skill

Agent-facing web search, URL extraction, single-page capture, and account credit-usage reporting backed by the [Firecrawl v2 API](https://docs.firecrawl.dev/api-reference/endpoint/search).

The search and extract commands mirror [tavily-skill](https://github.com/grapeot/tavily-skill): the same subcommands (`search`, `extract`), the same flag names and defaults, the same JSON envelope, and the same file-output behavior, with one exception: `search` and `extract` do not accept `--stdout`. Workflows that read the payload file still see that envelope. `usage` and `scrape` are this repo's own additions — check them with `--help` before assuming any other flag names. `scrape` is the single-page structural primitive (one URL, one format, required `--output`) and is specified in [`docs/rfc.md`](docs/rfc.md) D9.

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

# many independent queries in one process, run in parallel (4 at a time by default)
python -m firecrawl_skill search \
  --query "latest AI news" \
  --query "openai releases" \
  --query "anthropic news"

# restrict to specific domains
python -m firecrawl_skill search "agent framework" \
  --include-domain github.com \
  --include-domain docs.anthropic.com

# URL content extraction
python -m firecrawl_skill extract https://example.com
python -m firecrawl_skill extract https://example.com --query "agent search"

# extraction with links and main-content cleaning
python -m firecrawl_skill extract https://example.com --links --only-main-content

# single-page capture: exactly one format, required --output, no snapshot corpus
python -m firecrawl_skill scrape https://example.com --raw-html --output /tmp/example.html
python -m firecrawl_skill scrape https://example.com --html --include-tags main --output /tmp/example-main.html
python -m firecrawl_skill scrape https://example.com --screenshot --full-page --output /tmp/example.png

# check remaining account credits (read-only, costs 0 credits)
python -m firecrawl_skill usage --stdout

# current balance plus the last 6 billing periods of consumption
python -m firecrawl_skill usage --history 6 --stdout
```

## Configuration

| Variable | Description |
|---|---|
| `FIRECRAWL_API_KEY` | Firecrawl API key (`fc-...`). Takes priority. |
| `ONEPASSWORD_FIRECRAWL_REFERENCE` | Optional `op read`-compatible reference (e.g. `op://Vault/Item/field`). Used when `FIRECRAWL_API_KEY` is not set. Requires the 1Password CLI. |
| `FIRECRAWL_CLI_OUTPUT_DIR` | Directory for auto-named payload files. Default: `./tmp/firecrawl/` (relative to the working directory). Point this at one dedicated, persistent directory so every payload accumulates as a timestamped corpus. `scrape` ignores this variable: it writes only the required `--output` path. |

A local `.env` file in the working directory (or any parent directory) is loaded automatically.

## Commands

| Command | Use it for | Input | Output |
|---|---|---|---|
| `search` | Discovery: find pages for a query | query text (single or batch) | full JSON payload file + status object |
| `extract` | Batch content: markdown/highlights for 1–20 URLs | URL list | full JSON payload file + status object |
| `scrape` | Single-page structure: raw HTML, cleaned HTML, or a screenshot | exactly one URL | the captured bytes at the required `--output`; status object on stdout |
| `usage` | Remaining credits and per-period consumption | — | full payload (`--stdout`) or file + status object |

### `search`

Web search via `POST /v2/search`. With `--raw-content markdown` (default), every result includes full-page markdown, which costs 1 additional Firecrawl credit per page on top of 2 credits per 10 results.

A single query is the positional form: `search "some query"`. An agent that needs many independent queries can launch them all in one process with the repeatable `--query` option — each `--query` is one complete query string (multi-word queries are never split):

```bash
python -m firecrawl_skill search --query "q1" --query "q2" --query "q3"
```

Batch mode runs the queries in parallel with `concurrent.futures.ThreadPoolExecutor` (no new dependencies), at `--concurrency N` workers (default 4) or sequentially with `--serial`. `--serial` wins over `--concurrency` and is equivalent to `--concurrency 1`. The positional query and `--query` are mutually exclusive; giving both, or neither, is a usage error (exit 2). `--output` applies only to single-query search and is rejected in batch mode. `search` never accepts `--stdout`.

Batch mode preserves the one-query-one-file corpus invariant: each query writes its own full-payload JSON file under `FIRECRAWL_CLI_OUTPUT_DIR` (or `./tmp/firecrawl/`), auto-named `search_{timestamp}_{slug}.json` with an index suffix on slug collision. Stdout carries exactly one batch status object — one entry per query plus aggregate counts and summed credits:

```json
{
  "command": "search",
  "status": "partial",
  "output_mode": "batch",
  "output_dir": "/path/to/output",
  "input": { "queries": ["q1", "q2", "q3"], "concurrency": 4, "serial": false, "...": "shared search flags" },
  "summary": { "query_count": 3, "success_count": 2, "failed_count": 1, "credits_used": 16 },
  "results": [
    { "query": "q1", "output_path": "/path/to/output/search_..._q1.json", "summary": { "result_count": 6, "credits_used": 8 }, "error": null },
    { "query": "q2", "output_path": "/path/to/output/search_..._q2.json", "summary": { "result_count": 6, "credits_used": 8 }, "error": null },
    { "query": "q3", "output_path": null, "summary": null, "error": { "http_status": 500, "error": "HTTP 500" } }
  ],
  "payload_schema": { "...": "one entry per envelope field, mirroring the single-mode convention" }
}
```

`status` is `ok` when every query succeeds, `partial` when some fail, and `error` when all fail. A partial failure still exits 0, with each failed query reported in the status and a `Warning:` line on stderr, exactly like `extract`'s partial failure; only the all-failed `error` case exits with the first failure's mapped code (10/11/12/13). Batch credits sum only the successful queries — in the example above the two successes contribute 8 + 8 = 16, and the failed query adds nothing. The batch status object is lightweight (no raw content inline) and carries a `payload_schema` hint; full result content lives in the per-query files.

### `extract`

URL content extraction via `POST /v2/scrape`, one request per URL (1–20 URLs per call). `--query` requests query-relevant highlights for each page.

`--links` adds a `links` array to every successful per-URL result. It is off by default to keep payloads small, stays present when combined with `--query`, and is an empty list when upstream returns none. `--only-main-content` explicitly sends `onlyMainContent: true`, which is already Firecrawl's default, so it does not change results and does not filter `links`; `--no-only-main-content` sends `onlyMainContent: false` to request the full page. The two are mutually exclusive, and omitting both leaves the request body unchanged. None of these flags changes any existing result field, `failed_results`, or exit code.

```bash
python -m firecrawl_skill extract https://example.com --links --only-main-content
python -m firecrawl_skill extract https://example.com --links --no-only-main-content
```

### `scrape`

Single-page structural capture via `POST /v2/scrape`: exactly one URL and exactly one of `--raw-html` (`rawHtml`), `--html` (cleaned HTML), or `--screenshot`. `--output PATH` is required for every format; its parent must already exist and be writable, and the target must not be an existing directory. These checks run before any request, and `scrape` never creates directories.

`scrape` is deliberately outside the snapshot corpus: it never writes `FIRECRAWL_CLI_OUTPUT_DIR`, never auto-names a file, and never adds a timestamped entry. The only file it writes is `--output`. Stdout still carries exactly one JSON status object; `--stdout` is rejected (exit 2), and the captured bytes never print inline.

```bash
python -m firecrawl_skill scrape https://example.com --raw-html --output /tmp/example.html
python -m firecrawl_skill scrape https://example.com --html --include-tags main --output /tmp/example-main.html
python -m firecrawl_skill scrape https://example.com --screenshot --full-page --viewport 1440x900 --output /tmp/example.png
```

- `--include-tags` / `--exclude-tags` (repeatable CSS selectors) are valid only with `--html`; silently filtering `rawHtml` would make "raw" misleading.
- `--full-page` and `--viewport WxH` are valid only with `--screenshot` (`WxH` uses positive integers).
- `--max-age MS` (non-negative), repeatable `--header 'Key: Value'` (split on the first colon; values are never logged), `--max-bytes N` (default 5 MiB for HTML, 20 MiB for screenshots; exceeding it exits 12 rather than truncating), and `--timeout` (seconds, default 60) are supported.
- A screenshot response is a temporary URL, so the CLI downloads it, verifies the PNG signature, and writes atomically. A failed download exits 13 and leaves no partial file.
- There are no actions, no LLM formats (`json`/`summary`/`question`), and no batch mode.

Full flag reference: `python -m firecrawl_skill search --help` / `extract --help` / `scrape --help`, or see `skills/skill_firecrawl.md`.

### `usage`

Account credit balance via `GET /v2/team/credit-usage` (read-only, consumes **0** credits). Flags: `--timeout` (default 60), `--history N` (1–100 closed billing periods of per-period consumption, from `GET /v2/team/credit-usage/historical`; `0` = current period only), `--stdout`, `--output`. No estimated-credits line is printed for this command — there is nothing to estimate.

```bash
python -m firecrawl_skill usage --stdout
```

```json
{
  "command": "usage",
  "input": { "timeout": 60, "history": 0, "stdout": true, "output": null },
  "data": {
    "provider": "firecrawl",
    "remaining_credits": 4200,
    "plan_credits": 5000,
    "credits_used_in_period": 800,
    "billing_period_start": "2025-01-01T00:00:00.000Z",
    "billing_period_end": "2025-02-01T00:00:00.000Z",
    "periods": [],
    "raw": { "credit_usage": { "success": true, "data": { "remainingCredits": 4200, "planCredits": 5000 } } }
  }
}
```

Normalized top-level fields mirror only what the upstream API returns; `credits_used_in_period` is derived as `plan_credits - remaining_credits` and is `null` when either input is missing. `data.raw` keeps the full upstream body verbatim, so every normalized field is auditable. `data.periods` holds `{start_date, end_date, credits_used}` per closed billing period (`end_date` is `null` for an open period) and stays empty without `--history`.

## Performance

For independent queries, batch mode is faster than N standalone invocations. Each standalone `python -m firecrawl_skill search ...` pays the fixed per-process cost — interpreter startup, module import, TLS setup, and API-key resolution — and then waits on one API round-trip; batch mode pays that fixed cost once and runs the queries as a single parallel wave. At N=4 the win is typically several times the wall-clock of one call, not a constant shave. `--concurrency` controls the wave width and `--serial` runs the same batch one-at-a-time as a control. [`benchmarks/latency.py`](benchmarks/latency.py) reproduces the numbers end to end (opt-in via `RUN_FIRECRAWL_LATENCY=1`; it spends real credits).

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

Every search and extract writes the complete payload to disk and emits a lightweight status object on stdout. Pass `--output PATH` to write that payload to a specific file instead of the default directory. For search, this applies only to single-query mode; batch search rejects it. `usage` also accepts `--output PATH`, mutually exclusive with `--stdout`. The full payload prints to stdout only for `usage --stdout`. See [Reading results from disk](skills/skill_firecrawl.md#reading-results-from-disk) for an executable status → exact path → JSON reader covering single search, extract, and batch search, with exit-code checks, bounded previews, and indexed range expansion.

`scrape` follows the same stdout contract but not the corpus behavior: `--output` is mandatory, the file contains the captured HTML or image bytes rather than a JSON payload, and the status object is `{command, status, output_mode: "file", output_path, payload_bytes, metadata, summary: {url, format, credits_used}, payload_schema}`. `metadata` is the upstream metadata object or `null`; `output_path` echoes the path exactly as given.

### Cost breakdown

`search` and `extract` payloads carry a `data.cost_breakdown` object that reconstructs the Firecrawl credit charge per document, so a finished payload explains its own bill:

```json
{
  "base": 2,
  "documents": [
    {"url": "https://x.com/x/status/1", "kind": "x", "credits": 30},
    {"url": "https://a/visa.pdf", "kind": "pdf", "pages": 67, "credits": 67},
    {"url": "https://b.com", "kind": "html", "credits": 1}
  ],
  "modelled_total": 100,
  "reported_total": 100,
  "reconciles": true,
  "warning": null
}
```

Credit cost is not linear: a plain HTML page is 1 credit, an x.com/twitter.com result is 30 (Grok), and a PDF is 1 credit per page. The breakdown makes that visible after the fact. It is best-effort and never blocks — on any disagreement it fills `warning` and leaves `credits_used` authoritative. The envelope shape above is the `search`/`extract` contract; `usage` reuses the `{command, input, data}` envelope with its own `data` block — see [`usage`](#usage) above.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 2 | Usage error (bad arguments) |
| 10 | Authentication failed (401/403) |
| 11 | Quota or rate limit (402/429) |
| 12 | Request rejected, no data (other 4xx) |
| 13 | Timeout or server error (408/5xx/network) |

The mapping is identical for `usage`: a documented `404 Could not find credit usage information` maps to 12, an unreachable or 5xx billing endpoint to 13.

`scrape` adds no codes. Two local conditions reuse the table: an output larger than `--max-bytes` maps to 12 (rejected, no partial file), and a failed screenshot download maps to 13 (network/server class, no partial file).

`search` batch mode is the one case where exit 0 also covers partial failure: when some `--query` values fail and at least one succeeds, the command exits 0 and the failures are reported per query in the status object (plus a stderr warning). If every query fails, the exit code is the first failure's mapped value.

## Credit accounting

| Command | Credits |
|---|---|
| `search` | 2 per 10 results, + 1 per scraped page when `--raw-content` is on |
| `extract` | 1 per page, + 4 per page with `--query` (highlights) |
| `scrape` | the upstream per-page scrape charge; `summary.credits_used` reports the actual figure |
| `usage` | 0 (read-only billing endpoint) |

`usage` is the only way to see your remaining balance from the CLI; it never costs credits. Firecrawl's plan name is not returned by any endpoint, so it is not reported — `plan_credits` is the plan's credit allotment and is the closest available signal.

## Differences from tavily-skill

Documented upstream gaps and how this CLI handles them: `docs/rfc.md` ("Capability gaps"). In short: search depth tiers are ignored with a warning, image descriptions and the `finance` topic are rejected, and domain include/exclude cannot be combined (Firecrawl constraint).

## Tests

```bash
python -m pytest tests/ -v                      # offline unit tests
RUN_FIRECRAWL_INTEGRATION=1 python -m pytest tests/test_integration.py -v   # live, consumes credits
```

## License

MIT — see [LICENSE](LICENSE).

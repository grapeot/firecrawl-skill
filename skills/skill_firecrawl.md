# Firecrawl Skill

Real-time web search, URL content extraction, and account credit-usage reporting through the Firecrawl v2 API. The CLI defaults to writing full payloads to local JSON files and returning a lightweight status object on stdout; use `--stdout` when you need the complete JSON inline.

The `search` and `extract` command surface and output contract mirror tavily-skill, so workflows switch by changing the module name only. `usage` is a firecrawl-skill addition with its own `data` block (Firecrawl has no credits-usage equivalent in tavily).

## When to use

Trigger when the user expresses any of these intents:

- Look up the latest information, news, or web content
- Search a topic and keep structured JSON results
- Need to limit result count, time range, or domain scope
- Need image results (no LLM image descriptions — Firecrawl does not provide them)
- Already have a URL and want to extract its body content directly
- Any scenario suited for real-time web search via Firecrawl
- Ask how many Firecrawl credits are left, how much was used this billing period, or whether a budget/plan threshold has been crossed (`usage`)

## Prerequisites

- Entry point: `python -m firecrawl_skill` (after installing the package from this repository root)
- Python dependencies: none (standard library only); dev extras via `pip install -e ".[dev]"`
- API key: `FIRECRAWL_API_KEY` takes priority; optionally `ONEPASSWORD_FIRECRAWL_REFERENCE` (value is an `op read`-compatible reference; never commit private vault paths to a public repository)

## First-time setup

On the first use in a workspace, check whether `FIRECRAWL_CLI_OUTPUT_DIR` is set (in the repo's `.env` or the ambient environment). If it is not set, mention this to the user once during setup — not at runtime — and recommend pointing it at one dedicated, persistent directory (for example a knowledge-base `web_snapshots/firecrawl_raw/` folder). The default `./tmp/firecrawl/` is ephemeral and resolves relative to the current working directory, so payloads scatter across session directories and get cleaned up; a single stable directory lets every search/extract payload accumulate as a timestamped corpus of primary sources, which pays off over time for research, auditing, and later retrieval.

## Usage

### Basic search

```bash
python -m firecrawl_skill search "latest AI news"
```

Defaults request `raw_content="markdown"` (full-page markdown per result, 1 Firecrawl credit per page on top of 2 credits per 10 results) and writes the complete result to an auto-named file under `tmp/firecrawl/` (or `FIRECRAWL_CLI_OUTPUT_DIR` when set); stdout returns only a status JSON.

### Specify result count and time range

```bash
python -m firecrawl_skill search "openai releases" --max-results 10 --time-range month
```

### Restrict to specific domains

```bash
python -m firecrawl_skill search "agent framework" \
  --include-domain github.com \
  --include-domain docs.anthropic.com
```

Include and exclude domain filters cannot be combined (Firecrawl constraint). To both target and exclude, use the `-site:` operator inside the query text.

### Write to a named file

```bash
python -m firecrawl_skill search "AI coding tools" --output /tmp/firecrawl_search.json
```

### Print full JSON directly to stdout

```bash
python -m firecrawl_skill search "AI coding tools" --stdout
```

### URL content extraction

```bash
python -m firecrawl_skill extract https://firecrawl.dev
python -m firecrawl_skill extract https://firecrawl.dev --query "agent search" --output /tmp/firecrawl_extract.json
```

`--query` requests query-relevant highlights for each page (Firecrawl returns the relevant passages; there is no chunk-count control).

### Disable images or raw content

```bash
python -m firecrawl_skill search "earnings news" --stdout --no-images --raw-content off
```

### Explicitly enable images

```bash
python -m firecrawl_skill search "latest Apple event stage photos" --images
```

Image results carry URLs and dimensions only; `--image-descriptions` is not supported and will be rejected.

### Check remaining credits

```bash
python -m firecrawl_skill usage --stdout
```

Reads the Firecrawl billing endpoint and costs **0 credits**, so it is safe to call before a large crawl or from a workflow that wants to bail out early. `--history N` (1–100) adds one entry per closed billing period from the historical endpoint — a second read-only call, also free.

```bash
python -m firecrawl_skill usage --history 6 --output /tmp/firecrawl_usage.json
```

Check `data.remaining_credits` before a batch you expect to be expensive: at ~8 credits per full-content 6-result search, a balance in the low hundreds is already too small for a crawl, and a `search` call that runs out returns exit code 11 rather than partial results.

## Default behavior

- `max_results=6` (1–20)
- `topic="general"`
- `raw_content="markdown"`
- `include_images` disabled by default
- If using 1Password: set `ONEPASSWORD_FIRECRAWL_REFERENCE` to point at the credential field
- Default mode writes the complete result to an auto-named file under `tmp/firecrawl/` (or under `FIRECRAWL_CLI_OUTPUT_DIR` when set); stdout prints a lightweight status object with `payload_schema`, and hints (including an estimated credit cost) go to stderr
- With `--output`, the complete result writes to the specified file; stdout still prints the lightweight status object
- With `--stdout`, the complete payload prints directly to stdout without writing to disk
- `credits_used` in `data` records the actual Firecrawl credits consumed (from the API response)
- `data.cost_breakdown` explains that number per document (HTML 1, x.com/twitter 30, PDF 1/page, unreturned 0); best-effort, non-blocking, with a `warning` on mismatch
- `usage` is the exception to the estimates above: it costs 0 credits and prints no `Estimated Firecrawl credits:` line

## Parameter reference

### `search`

| Parameter | Description | Default |
|---|---|---|
| `query` | Search query | required |
| `--max-results` | Number of results, range 1–20 | `6` |
| `--search-depth` | `basic` / `advanced` / `fast` / `ultra-fast` (accepted for compatibility; Firecrawl has a single search mode, so the value is ignored with a stderr warning) | `advanced` |
| `--topic` | `general` / `news` (`finance` is rejected; use `--include-domain` on finance sites) | `general` |
| `--time-range` | `day` / `week` / `month` / `year` | — |
| `--start-date` | Start date, `YYYY-MM-DD` (paired with `--end-date`) | — |
| `--end-date` | End date, `YYYY-MM-DD` (paired with `--start-date`) | — |
| `--include-domain` | Restrict to a domain; repeat for multiple (mutually exclusive with `--exclude-domain`) | — |
| `--exclude-domain` | Exclude a domain; repeat for multiple (mutually exclusive with `--include-domain`) | — |
| `--stdout` | Print full payload directly to stdout | `False` |
| `--raw-content` | `off` / `markdown` / `text` (`text` maps to `markdown`) | `markdown` |
| `--country` | Geo-target boost, for example `US` or `Germany` | — |
| `--timeout` | Request timeout in seconds | `60` |
| `--images` | Enable image results (limit applies per source) | `False` |
| `--no-images` | Disable image results | `False` |
| `--output` | Write full result to a named JSON file; stdout still returns status schema | auto-writes to `tmp/firecrawl/` or `FIRECRAWL_CLI_OUTPUT_DIR` |

### `extract`

| Parameter | Description | Default |
|---|---|---|
| `urls...` | One or more URLs, up to 20 (scraped sequentially) | required |
| `--format` | `markdown` / `text` (`text` maps to `markdown`) | `markdown` |
| `--query` | Request query-relevant highlights per page | — |
| `--extract-depth` | `basic` / `advanced` (accepted for compatibility; always ignored with a warning) | `advanced` |
| `--chunks-per-source` | Accepted for compatibility; always ignored with a warning | — |
| `--images` | Enable image extraction | `False` |
| `--no-images` | Disable image extraction | `False` |
| `--favicon` | Pass favicon URL through from metadata when present | `False` |
| `--timeout` | Per-URL request timeout in seconds | `60` |
| `--stdout` | Print full payload directly to stdout | `False` |
| `--output` | Write full extract payload to a named JSON file; stdout still returns status schema | auto-writes to `tmp/firecrawl/` or `FIRECRAWL_CLI_OUTPUT_DIR` |

### `usage`

| Parameter | Description | Default |
|---|---|---|
| `--history` | Include the N most recent closed billing periods of credit consumption (0–100; 0 = current period only) | `0` |
| `--timeout` | Request timeout in seconds | `60` |
| `--stdout` | Print full payload directly to stdout | `False` |
| `--output` | Write full usage payload to a named JSON file; stdout still returns status schema | auto-writes to `tmp/firecrawl/` or `FIRECRAWL_CLI_OUTPUT_DIR` |

There is no positional argument and no `--format`: the balance is a small structured object, not page content.

## Image guidance

Images are off by default. The reason is not that images lack value — it's that most research and survey workflows don't consume `data.images`, and enabling them silently inflates payload size.

Enable explicitly in these scenarios:

- Writing an external report or newsletter that needs accompanying images
- The topic is inherently visual — UI, hardware appearance, satellite imagery, document samples, news event photos
- You explicitly need image search, not factual web retrieval

Keep disabled in these scenarios:

- Routine fact-checking, news aggregation, product/company research
- Sub-agent research where token and output volume compression matters
- Downstream workflows that only consume URLs, text excerpts, and structured conclusions

## Output structure

The top-level structure is fixed:

```json
{
  "command": "search",
  "input": {},
  "data": {
    "query": "...",
    "results": [],
    "images": [],
    "credits_used": 8,
    "result_count": 0,
    "image_count": 0
  }
}
```

`search`'s `data.results` retains one item per web result with `url`, `title`, `description`, `position`, `raw_content` (markdown when requested, `null` otherwise), optional `links` and `metadata`. News items also land in `data.news`; with `--topic news` they are merged into `data.results` as well. For `extract`, `data.results` holds one item per URL (`markdown`, optional `highlights`/`images`/`metadata`) and additionally carries `failed_results` and `failed_count`.

Both commands also carry `data.cost_breakdown`, which reconstructs the credit charge per document (`base`, `documents[]` with `kind`/`credits`, `modelled_total`, `reported_total`, `reconciles`, `warning`). Credit cost is not linear — HTML is 1 credit, x.com/twitter.com is 30 (Grok), PDFs are 1 credit/page, and an unreturned document is 0 — so this field explains a finished payload's bill. It is best-effort and never blocks: on disagreement it sets `reconciles: false` and fills `warning`, while `credits_used` stays authoritative.

For `usage`, `data` carries the balance instead of results:

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

`credits_used_in_period` is a derived value (`plan_credits - remaining_credits`), `null` when either input is missing; `raw` holds the upstream body verbatim for auditing. `periods` entries are `{start_date, end_date, credits_used}` (`end_date` is `null` while a period is still open) and are only populated with `--history`. Firecrawl returns no plan name, so none is reported. With `--output`, the usage status object reports the balance in `summary.remaining_credits` alongside the usual `summary.credits_used` (always `null` for this command).

In default mode, stdout does not return this full payload. It returns a lightweight object containing the output path, summary information (including `credits_used`), and payload schema. The full payload only prints to stdout when `--stdout` is passed.

## Testing

Run unit tests only (default, offline):

```bash
python -m pytest tests/ -v
```

Run paid integration tests explicitly:

```bash
RUN_FIRECRAWL_INTEGRATION=1 FIRECRAWL_API_KEY=fc-... python -m pytest tests/ -v -m integration
```

Integration tests hit the real Firecrawl API and consume credits. If `FIRECRAWL_API_KEY` is not set, configure `ONEPASSWORD_FIRECRAWL_REFERENCE` and ensure the local `op` CLI is available.

## Notes

- `--time-range` and `--start-date`/`--end-date` are mutually exclusive — use one or the other
- `--include-domain` and `--exclude-domain` are mutually exclusive (Firecrawl constraint)
- `--image-descriptions` and `--topic finance` are rejected with a usage error
- The currently stable commands are `search`, `extract`, and `usage`
- `usage` reads a billing endpoint: 0 credits, and its normalized fields exist only where the API returns values (`data.raw` keeps the upstream body verbatim)
- `--output` still produces JSON on stdout, but that stdout is the status schema, not the full search result
- Exit codes: 0 ok / 2 usage error (bad arguments) / 10 auth / 11 quota-rate / 12 rejected-no-data / 13 network-server — the same table for all three commands, `usage` included

## Operational guidance

- Run the CLI from this skill repo's root directory using its project-local interpreter: `./.venv/bin/python -m firecrawl_skill ...`. Do not run it from a parent workspace root, because the parent environment may not load this repo's `.env`, so `ONEPASSWORD_FIRECRAWL_REFERENCE` / `FIRECRAWL_API_KEY` may be missing even though sub-agents or project-local calls work.
- If you want to consume results directly in the current turn rather than writing to disk first, pass `--stdout`. Otherwise stdout only returns a lightweight status object, and the full payload lands under `tmp/firecrawl/` (or `FIRECRAWL_CLI_OUTPUT_DIR` when set).
- For routine research, default to `--raw-content markdown`. Base judgments on `data.results[*].raw_content`, source URLs, page titles, and snippet content.
- Only pass `--raw-content off` when payload size is a confirmed bottleneck (it is also materially cheaper upstream: 2 credits per 10 results instead of 2 + 1 per page).
- Mind the cost model: each full-content search at 6 results costs ~8 Firecrawl credits. Estimate credits are printed to stderr before the request; actual usage lands in `data.credits_used`.
- Check the balance with `python -m firecrawl_skill usage --stdout` (0 credits) before anything that scrapes many pages, and re-check after a large batch instead of adding up `data.credits_used` by hand. `usage --history 6` shows the trailing billing periods when you need a monthly burn rate rather than a snapshot.
- Because there are no implicit retries, an exit 11 (402/429) from `search` means the caller decides what to do next — confirm the balance with `usage` before retrying.

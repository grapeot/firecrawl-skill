# RFC — firecrawl-skill design

Status: accepted (2026-09-29). Supersedes the internal design memo of the same date where noted.

## Summary

A thin, stdlib-only Python CLI over the Firecrawl v2 API (`POST https://api.firecrawl.dev/v2/search`, `POST /v2/scrape`) that mirrors the tavily-skill command surface and output envelope, so downstream workflows switch by replacing the module name and the key.

## Key decisions

### D1 — Standard library transport (no runtime dependencies)

`urllib.request` handles everything needed: POST with a JSON body, `Authorization: Bearer` header, per-request timeout, and HTTP status inspection. Following the track17-skill invariant ("standard library only in `src/`") keeps the public repo dependency-free and the install trivial.

Supersedes the internal memo's `httpx` decision. Rationale: the two endpoints used are simple enough that an HTTP client library buys little; a pinned raw request body is also more stable against fast-moving upstream API changes (parameters fail loudly instead of silently dropping).

### D2 — Envelope kept from tavily-skill (no upstream `raw` passthrough)

Envelope: `{command, input, data}`. Search: `data.results` (from upstream `data.web` or `data.news`), `data.images`, `data.news`, `data.credits_used`, `data.result_count`, `data.image_count`. Extract: `data.results`, `data.failed_results`, `data.credits_used`, `data.result_count`, `data.failed_count`, `data.image_count`.

Deviation from the track17-skill envelope (`{command, input, data, error}` with `data.raw`): tavily-skill has no `raw` passthrough and its consumers read `data.results[*].raw_content`; duplicating the upstream body would double payload size for no consumer benefit. The `raw_content` field name is preserved on each result even though upstream calls it `markdown` — this is the single most important field-name decision in the project.

### D3 — `extract` is a sequential loop over `/v2/scrape`

`/v2/batch/scrape` is an asynchronous job (returns a job id, requires polling a status endpoint). For 1–20 URLs a sequential loop over the synchronous `/v2/scrape` endpoint is simpler, deterministic, and within rate limits. Failures land in `data.failed_results` and do not abort the loop.

### D4 — `.env` loading without python-dotenv

A minimal built-in loader (parse `KEY=VALUE` lines, skip comments/blank lines, no override of existing environment variables) walks the current directory and its parents, first `.env` found wins. Same traversal behavior as tavily-skill, stdlib only.

## API mapping — `search`

Upstream: `POST /v2/search`.

| CLI flag | Firecrawl request field | Notes |
|---|---|---|
| `query` | `query` | required, ≤500 chars |
| `--max-results` (1–20, default 6) | `limit` | API allows 1–100; CLI caps at 20 to control cost |
| `--raw-content markdown` / `text` | `scrapeOptions: {formats: ["markdown"]}` | +1 credit per result page. `text` maps to `markdown` (API has no text tier); stderr note. `off` → no `scrapeOptions` |
| `--time-range day/week/month/year` | `tbs` = `qdr:d` / `qdr:w` / `qdr:m` / `qdr:y` | applies to the `web` source only (upstream behavior) |
| `--start-date` / `--end-date` | `tbs` = `cdr:1,cd_min:MM/DD/YYYY,cd_max:MM/DD/YYYY` | CLI converts ISO dates to `MM/DD/YYYY`; missing side → usage error |
| `--include-domain` (repeatable) | `includeDomains` | hostnames only, no scheme/path |
| `--exclude-domain` (repeatable) | `excludeDomains` | mutually exclusive with `includeDomains` upstream → CLI usage error if both given |
| `--topic general` | `sources: ["web"]` | default |
| `--topic news` | `sources: ["news"]` | results normalized from `data.news` (`snippet` → `description`, `date` kept) |
| `--topic finance` | — | usage error; suggest `--include-domain` on finance domains |
| `--images` | `sources` += `"images"` | `limit` applies per source (upstream); stderr note |
| `--image-descriptions` | — | usage error (upstream image results have no description field) |
| `--country` | `location` (free text) and `country` (ISO code when inferrable) | both set when possible (upstream recommendation) |
| `--timeout` (s, default 60) | `timeout` (ms, ×1000) | upstream max 300000 ms |
| `--search-depth` (any of `basic`/`advanced`/`fast`/`ultra-fast`) | — | accepted, ignored, warning on stderr, recorded in `input` (upstream has no depth tiers) |
| `--stdout` / `--output` | — | identical semantics to tavily-skill |

Request body is otherwise minimal: no `categories`, no `enterprise`, no `threatProtection`. `highlights` stays at its upstream default (`true`).

### Response normalization (search)

- `data.web[]` → `data.results[]`, keeping `url`, `title`, `description`, `position`, `markdown` → `raw_content`, `links`, `metadata`. Missing `markdown` (content not requested or scrape failed) → `raw_content: null`.
- `data.news[]` → `data.news[]` (kept as its own array); when `--topic news`, `data.news` items are also merged into `data.results` (with `snippet` → `description`) so consumers have one array to read.
- `data.images[]` → `data.images[]` (`imageUrl` kept; no description field exists upstream).
- `creditsUsed` → `data.credits_used` (integer; `null` if absent).

## API mapping — `extract`

Upstream: `POST /v2/scrape`, one request per URL, sequential.

| CLI flag | Firecrawl request field | Notes |
|---|---|---|
| `urls` (1–20) | one request per URL | failures recorded in `data.failed_results` with the upstream error string; loop continues |
| `--format markdown` / `text` | `formats: ["markdown"]` | `text` maps to `markdown` |
| `--query` | `formats` += `{"type": "highlights", "query": <q>}` | upstream returns the page's relevant passages in `data.highlights`; closest equivalent to tavily's query-chunked extract |
| `--chunks-per-source` | — | accepted, warned, ignored (highlights do not cap passage count) |
| `--extract-depth` | — | accepted, warned, ignored (scrape always fully renders) |
| `--images` | `formats` += `"images"` | image URLs found on the page |
| `--favicon` | — | not requested explicitly; `metadata.faviconUrl` passed through when present (verify field name against live responses during implementation) |
| `--timeout` (s, default 60) | `timeout` (ms) | per-URL timeout, upstream max 300000 ms |
| `--stdout` / `--output` | — | identical semantics to tavily-skill |

Per-URL result shape: `{url, markdown: <upstream data.markdown>, highlights, images, metadata, error: null}`; on failure: `{url, error: <upstream error string or HTTP status>}`. `data.credits_used` is the sum across successful scrapes (upstream `creditsUsed`/`metadata.creditCount` when present).

## API mapping — `usage`

Upstream: `GET /v2/team/credit-usage` (no request body). Reuses the resolved API key and the shared HTTP status → exit-code mapping.

| CLI flag | Firecrawl request field | Notes |
|---|---|---|
| `--timeout` (s, default 60) | — | GET request timeout |
| `--stdout` / `--output` | — | identical semantics to the other subcommands |

`data.remainingCredits` / `data.planCredits` / `data.billingPeriodStart` / `data.billingPeriodEnd` map to `data.remaining_credits` / `data.plan_credits` / `data.billing_period_start` / `data.billing_period_end`. The command consumes no credits and is not part of the credit-estimate stderr output.

## Validation rules

1. `--include-domain` and `--exclude-domain` are mutually exclusive (search).
2. `--time-range` and `--start-date`/`--end-date` are mutually exclusive (search).
3. `--start-date` and `--end-date` must be paired.
4. `--max-results` in 1–20; `--timeout` > 0 (search and extract).
5. `--stdout` and `--output` are mutually exclusive.
6. Extract URL count in 1–20.
7. `--image-descriptions`, `--topic finance` → usage error.
8. Invalid dates → usage error with the offending value.

## Exit codes and HTTP mapping

| HTTP / condition | Exit code |
|---|---|
| 200, `success: true` (or scrape data present) | 0 |
| argparse error | 2 |
| 401, 403 | 10 |
| 402, 429 | 11 |
| other 4xx (incl. `success: false` with 200) | 12 |
| 408, 5xx, socket timeout, DNS/ConnectionError | 13 |

Errors print a one-line JSON `{"command": ..., "error": ..., "http_status": ...}` to stderr and exit with the mapped code (stdout stays clean).

## Key management

Resolution order: `FIRECRAWL_API_KEY` → `ONEPASSWORD_FIRECRAWL_REFERENCE` (via `op read <ref>`, 10 s timeout, stderr hint on failure) → exit 10 with instructions. The `.env` loader runs before key resolution.

## Output files

Auto-named under `FIRECRAWL_CLI_OUTPUT_DIR` (default `./tmp/firecrawl/`): `{command}_{YYYYMMDD_HHMMSS}_{slug}.json`, slug from the query (search) or first URL (extract), same slug rules as tavily-skill. `--output` writes to the given path. Stdout status object: `{command, status: "ok", output_mode: "file", output_path, payload_bytes, summary: {result_count, failed_count, image_count, credits_used}, payload_schema}` plus a `Saved JSON to ...` line on stderr.

## Capability gaps (user-facing behavior)

| Gap | Trigger | Behavior | Suggested workaround |
|---|---|---|---|
| No search depth tiers | `--search-depth` | warn + ignore | Firecrawl's single mode + highlights covers most "deep" needs |
| No image descriptions | `--image-descriptions` | usage error | run a vision model over `imageUrl` |
| No finance vertical | `--topic finance` | usage error | `--include-domain` on finance sites |
| No chunked extraction | `--chunks-per-source` | warn + ignore | `--query` returns highlights; for a direct answer, ask the model over `raw_content` |
| Domain filters exclusive | include + exclude together | usage error | pick one, or use the `-site:` operator inside the query text (upstream supports query operators) |

## Testing strategy

- **Offline (`tests/test_unit.py`)**: argparse validation matrix (all rules above), ISO → `MM/DD/YYYY` conversion, `tbs` construction, request-body builders (search and extract, every flag combination that matters), envelope normalization from recorded fixtures, `usage` normalization and error mapping (transport monkeypatched), exit-code mapping from simulated HTTP responses. No network, no key.
- **Live (`tests/test_integration.py`, opt-in via `RUN_FIRECRAWL_INTEGRATION=1`)**: 3–5 real calls (plain search, search + content, `--time-range`, extract with `--query`), asserting `success`, `credits_used` present, `raw_content` non-empty. Consumes a small number of credits.

## Open items to verify against live responses during implementation

- Exact `/v2/scrape` response schema and where `creditsUsed`/`creditCount` live.
- Favicon field name in `metadata`.
- Whether `--topic news` + content scraping behaves identically to web (schema indicates `markdown` on news items; confirm).

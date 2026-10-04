# RFC — firecrawl-skill design

Status: accepted (2026-09-29). Supersedes the internal design memo of the same date where noted.

## Summary

A thin, stdlib-only Python CLI over the Firecrawl v2 API (`POST https://api.firecrawl.dev/v2/search`, `POST /v2/scrape`, plus `GET /v2/team/credit-usage` for the `usage` subcommand) that mirrors the tavily-skill command surface and output envelope, so downstream workflows switch by replacing the module name and the key.

## Key decisions

### D1 — Standard library transport (no runtime dependencies)

`urllib.request` handles everything needed: POST with a JSON body, `Authorization: Bearer` header, per-request timeout, and HTTP status inspection. Following the track17-skill invariant ("standard library only in `src/`") keeps the public repo dependency-free and the install trivial.

`usage` needs the same transport as a GET (no body, `Authorization` header, per-request timeout), so `_get_json` was added beside `_post_json` and both share one JSON-body parser. Still stdlib `urllib.request`.

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

Upstream: `GET https://api.firecrawl.dev/v2/team/credit-usage` (the docs page is `/api-reference/endpoint/credit-usage`; the v2 OpenAPI registers the path under `/team/`).

### D5 — credit-usage endpoint path is `/v2/team/credit-usage`

Verified empirically on 2026-10-03 against the live API with a real key:

| Candidate | Result |
|---|---|
| `GET /v2/credit-usage` | **404** `{"success":false,"code":"NOT_FOUND","error":"GET /v2/credit-usage is not a Firecrawl API endpoint."}` |
| `GET /v2/billing/usage` | **404** same `NOT_FOUND` envelope |
| `GET /v2/usage` | **404** same `NOT_FOUND` envelope |
| `GET /v2/team/credit-usage` | **200** `{"success":true,"data":{…}}` |
| `GET /v2/team/credit-usage/historical` | **200** `{"success":true,"periods":[…]}` |

The upstream 404 envelope carries `code: "NOT_FOUND"` and a `documentation_url`, which distinguishes "wrong path" from "no credit info"; the CLI does not branch on it. The path is also cross-checked against the published `api-reference/v2-openapi.json`, which registers `/team/credit-usage`.

### D6 — historical consumption needs a second endpoint

`GET /v2/team/credit-usage` returns only the current period's balance, so per-period burn is only available from `GET /v2/team/credit-usage/historical` (`?byApiKey` is left at its default `false`, so no API-key names appear in payloads). The CLI uses this second endpoint **only** for `--history N`. Both calls are GETs on billing endpoints and consume 0 credits.

Field-name drift on the historical endpoint: the OpenAPI response schema names the per-period value `totalCredits`, the live response names it `creditsUsed`. `_normalize_period` reads `creditsUsed` first and falls back to `totalCredits`, so a rename in either direction leaves `credits_used` populated instead of silently `null`.

## Validation rules

1. `--include-domain` and `--exclude-domain` are mutually exclusive (search).
2. `--time-range` and `--start-date`/`--end-date` are mutually exclusive (search).
3. `--start-date` and `--end-date` must be paired.
4. `--max-results` in 1–20; `--timeout` > 0 (search and extract).
5. `--stdout` and `--output` are mutually exclusive.
6. Extract URL count in 1–20.
7. `--image-descriptions`, `--topic finance` → usage error.
8. Invalid dates → usage error with the offending value.
9. `usage`: `--timeout` > 0; `--history` in 0–100; `--stdout` and `--output` mutually exclusive.

## Normalization — `usage`

`data = {provider: "firecrawl", remaining_credits, plan_credits, credits_used_in_period, billing_period_start, billing_period_end, periods, raw}`.

- `remainingCredits` → `remaining_credits`, `planCredits` → `plan_credits`, `billingPeriodStart`/`billingPeriodEnd` → `billing_period_start`/`billing_period_end` (values passed through unchanged, including `null`).
- `credits_used_in_period` is the only derived field: `plan_credits - remaining_credits`, `null` when either input is absent. No other arithmetic.
- Firecrawl returns no plan **name** on any billing endpoint, so no plan name is reported; `plan_credits` is the nearest available signal.
- `periods[]` entries are exactly `{start_date, end_date, credits_used}`. `apiKey` is never projected into `periods` even if an upstream response includes it: the CLI always requests the default `byApiKey=false` and records `input.by_api_key: false`, so projecting a key name would contradict the recorded request. It remains visible in `data.raw`.
- `raw` keeps the full upstream body/ies (`raw.credit_usage`, plus `raw.credit_usage_historical` when fetched). This is a deliberate exception to D2 — the tavily-skill "no `raw` passthrough" rule exists because search payloads are megabytes; a billing body is ~165 bytes, and verbatim upstream text is what makes the normalized fields auditable.
- Status-object summary carries `remaining_credits` alongside the existing keys on every command (`null` for search/extract) so consumers read one summary shape.

## Cost breakdown (observability) — `cost_breakdown`

Firecrawl search is `base + per-document`; the per-document cost is not linear. The upstream `creditsUsed` is authoritative, but it does not say *why*. Every `search`/`extract` payload therefore carries a `data.cost_breakdown` object that reconstructs the charge so a finished payload can be audited after the fact:

```json
{
  "base": 2,
  "documents": [
    {"url": "https://x.com/x/status/1", "kind": "x", "credits": 30},
    {"url": "https://a/visa.pdf", "kind": "pdf", "pages": 67, "credits": 67},
    {"url": "https://b.com", "kind": "html", "credits": 1},
    {"url": "https://x.com/x/all", "kind": "x", "credits": 0}
  ],
  "modelled_total": 100,
  "reported_total": 100,
  "reconciles": true,
  "warning": null
}
```

Rules (verified against 212 recorded `search` calls and 164 `extract` calls from 2026-10; all reconcile exactly):

- `base` = `2 × ceil(result_count / 10)`. It keys off **results actually returned**, not `--max-results`: a query that matches nothing bills 0.
- A document that was **not returned** (no `raw_content` for search / no `markdown` for extract, e.g. an `x.com/.../all` list page) bills **0**.
- A returned **plain HTML** document bills **1** (`metadata.creditsUsed` when present, else 1).
- An **x.com / twitter.com** document bills **30** (`creditsUsed: 30`; 1 base + 29 Grok).
- A **PDF** document bills **1 × `metadata.numPages`** (the page count is copied into `pages`).
- `--query` on extract adds 4/page (`highlights`); the upstream per-page `creditsUsed` already folds this in, so it is read directly, not re-derived.

Host matching for the X/Twitter rule normalizes case and FQDN trailing dots and requires an exact-or-subdomain match, so `evilx.com`, `x.com.evil.com`, `notx.com`, and `x.com.cn` do **not** qualify. JSON booleans are rejected from billing arithmetic (a `true` credit field is not read as 1).

`cost_breakdown` is best-effort and **never blocks**. When `modelled_total != reported_total` it sets `reconciles: false` and fills `warning` with a human-readable sentence naming both figures; the command still succeeds and all other fields are unaffected. `credits_used` remains the source of truth. When the API returns no total, `reconciles` is `null` and no warning is emitted (nothing to reconcile against).

## Exit codes and HTTP mapping

| HTTP / condition | Exit code |
|---|---|
| 200, `success: true` (or scrape data present) | 0 |
| argparse error | 2 |
| 401, 403 | 10 |
| 402, 429 | 11 |
| other 4xx (incl. `success: false` with 200) | 12 |
| 408, 5xx, socket timeout, DNS/ConnectionError | 13 |

The table applies unchanged to `usage`: the documented `404 Could not find credit usage information` maps to 12, a 5xx or unreachable billing endpoint to 13. No new codes were added (R5).

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

- **Offline (`tests/test_unit.py`)**: argparse validation matrix (all rules above), ISO → `MM/DD/YYYY` conversion, `tbs` construction, request-body builders (search and extract, every flag combination that matters), envelope normalization from recorded fixtures, `cost_breakdown` reconstruction (base math, HTML/PDF/X/zero-doc classification, mismatch warning, absent-total), exit-code mapping from simulated HTTP responses (transport monkeypatched). No network, no key.
- **Live (`tests/test_integration.py`, opt-in via `RUN_FIRECRAWL_INTEGRATION=1`)**: 3–5 real calls (plain search, search + content, `--time-range`, extract with `--query`), asserting `success`, `credits_used` present, `raw_content` non-empty. Consumes a small number of credits. The `usage` live test adds one read-only billing call (0 credits) asserting `remaining_credits`/`plan_credits` are ints and `raw.credit_usage` agrees with the normalized fields; it skips when no key resolves.

## Open items to verify against live responses during implementation

- Exact `/v2/scrape` response schema and where `creditsUsed`/`creditCount` live.
- Favicon field name in `metadata`.
- Whether `--topic news` + content scraping behaves identically to web (schema indicates `markdown` on news items; confirm).

Resolved during the `usage` work (2026-10-03):

- Credit-usage path confirmed as `GET /v2/team/credit-usage`; `/v2/credit-usage`, `/v2/billing/usage` and `/v2/usage` are all 404 (D5).
- `POST /v2/search` carries no credit-balance fields in body or headers — only `creditsUsed` for the call itself. Response headers are `access-control-allow-origin`, `content-length`, `content-type`, `date`, `etag`, `x-response-time`; there is no `x-ratelimit-*` or remaining-credits header. The billing endpoint is therefore the only source for the balance, and it is not rate-limited for reads.

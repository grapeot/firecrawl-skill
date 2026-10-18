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
| `query` (positional, optional) / `--query` (repeatable) | `query` per request | positional = single mode; `--query` = batch mode (one request per value). Both or neither → usage error |
| `--concurrency N` / `--serial` | — | batch fan-out control; `--serial` forces 1 and overrides `--concurrency`; default 4 |
| `--stdout` / `--output` | — | `--output PATH` writes the full payload and emits a status object. `--stdout` is rejected (exit 2, before any request, no file, no credits). `--output` is single-mode only |

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
| `--links` | `formats` += `"links"` | every successful per-URL result gains `links` (a list; empty list when upstream omits it). The field appears only when the flag is set, and it stays present when combined with `--query` |
| `--only-main-content` | `onlyMainContent: true` | affects markdown cleaning only; `links` are not filtered by it |
| `--images` | `formats` += `"images"` | image URLs found on the page |
| `--favicon` | — | not requested explicitly; `metadata.faviconUrl` passed through when present (verify field name against live responses during implementation) |
| `--timeout` (s, default 60) | `timeout` (ms) | per-URL timeout, upstream max 300000 ms |
| `--stdout` / `--output` | — | `--output PATH` writes the full payload and emits a status object. `--stdout` is rejected (exit 2, before any request, no file, no credits) |

Per-URL result shape: `{url, markdown: <upstream data.markdown>, highlights, images, links, metadata, error: null}`; on failure: `{url, error: <upstream error string or HTTP status>}`. `highlights`, `images`, and `links` are present only when their flags request them; default payloads are unchanged. `data.credits_used` is the sum across successful scrapes (upstream `creditsUsed`/`metadata.creditCount` when present).

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

### D8 — multi-query batch mode for `search`

An agent workflow that needs N independent searches otherwise pays N CLI round-trips. Batch mode lets one invocation carry N queries and execute them internally in parallel. It stays stdlib-only via `concurrent.futures.ThreadPoolExecutor`.

- Interface: the positional `query` becomes optional (`nargs="?"`); a repeatable `--query` (dest `queries`, `action="append"`) selects batch mode. Each `--query` is one complete query string. Positional + `--query` → usage error; neither → usage error.
- Parallelism: `--concurrency N` (int ≥1, default 4) or `--serial` (forces 1, overrides `--concurrency`).
- Invariant preservation: one query still maps to one full-payload file under the default output dir, so the timestamped corpus invariant holds. `--stdout` is rejected for every search. `--output` is a usage error in batch mode. Auto-naming shares the single-mode `_slugify` + timestamp scheme and appends an index on collision.
- Stdout: exactly one batch status object (one entry per query with `query`/`output_path`/`summary`/`error`, plus `query_count`/`success_count`/`failed_count`/`credits_used` and a `payload_schema` hint), never raw content inline.
- Exit codes reuse the existing contract unchanged: 0 on success **including partial failure** (per-query errors in the status and a stderr warning, mirroring `run_extract` partial failure); all-fail returns the first failure's mapped code (10/11/12/13); argument errors stay 2. Batch `credits_used` sums successful queries only. No new codes.

### D9 — `scrape` is a single-format, file-output primitive outside the snapshot corpus

`scrape` captures one page in a structural form `extract` does not expose: unmodified `rawHtml`, cleaned `html`, or a screenshot. It intentionally does not reuse `extract`, because `extract` is the batch content command (1–20 URLs, markdown/highlights) whose payloads accumulate as the timestamped corpus. Three constraints define the command:

- **Envelope invariant wins.** Every command prints exactly one JSON object, so the captured bytes go to a required `--output` path and stdout carries only a status object. There is no auto-naming, no `FIRECRAWL_CLI_OUTPUT_DIR` resolution, and no corpus entry.
- **Exactly one format.** `--raw-html` / `--html` / `--screenshot` are mutually exclusive and one is required. There is no `--formats` and no multi-format payload. `--include-tags`/`--exclude-tags` apply only to `--html`: filtering `rawHtml` silently would misrepresent "raw", and a screenshot has no DOM to filter. `--full-page`/`--viewport` apply only to `--screenshot`.
- **Screenshots are downloaded.** Firecrawl returns a temporary URL for a screenshot rather than image bytes (the design records a 24-hour expiry). The CLI fetches it with stdlib `urllib.request`, enforces `--max-bytes`, verifies the PNG signature (first 8 bytes), and writes atomically (temp file then `os.replace`). An oversized payload exits 12; a failed download exits 13; neither leaves a partial file.

No actions, no LLM formats (`json`/`summary`/`question`), no `/extract`, no Interact, and no `/v2/batch/scrape`. `search`, `extract`, and `usage` behavior is unchanged except the two additive `extract` flags.

## API mapping — `scrape`

Upstream: `POST /v2/scrape`, one request per invocation.

| CLI flag | Firecrawl request field | Notes |
|---|---|---|
| `URL` (positional) | `url` | exactly one URL; no batch input |
| `--raw-html` | `formats: ["rawHtml"]` | unmodified HTML; mutually exclusive with the other format flags |
| `--html` | `formats: ["html"]` | cleaned HTML; the only format that accepts tag filters |
| `--screenshot` | `formats: ["screenshot"]` | response field is a temporary image URL, which the CLI downloads |
| `--include-tags SELECTOR` (repeatable) | `includeTags` | only with `--html`; otherwise usage error (exit 2) |
| `--exclude-tags SELECTOR` (repeatable) | `excludeTags` | only with `--html`; otherwise usage error (exit 2) |
| `--max-age MS` | `maxAge` | non-negative integer milliseconds |
| `--header 'Key: Value'` (repeatable) | `headers` | split on the first colon and trim both sides; empty key or value is a usage error; values are never logged or persisted |
| `--timeout SECONDS` (default 60) | `timeout` | converted to milliseconds (×1000) |
| `--full-page` | screenshot options `fullPage: true` | only with `--screenshot` |
| `--viewport WxH` | screenshot options `viewport: {width, height}` | only with `--screenshot`; both positive integers |
| `--max-bytes N` | — | local cap only: 5 MiB default for `html`/`rawHtml`, 20 MiB for `screenshot`; exceeding it exits 12, never truncates |
| `--output PATH` | — | required local destination; parent must exist and is not created |
| `--stdout` | — | rejected (exit 2) before any request |

`onlyMainContent` is not set for `scrape`. Screenshot options are sent as a screenshot object (`{"type": "screenshot", "fullPage": true, "viewport": {"width": W, "height": H}}`) only when the corresponding flags are present. Verify this request shape against the live v2 schema during implementation; if it differs, follow the live schema and record the difference under Lessons Learned in `docs/working.md`.

### Response normalization (scrape)

- `rawHtml` mode: read `data.rawHtml`; verify the field name against live responses, since some responses may expose the cleaned variant as `data.html` and the raw variant under a different key.
- `html` mode: read `data.html`.
- `screenshot` mode: `data.screenshot` is a URL. Download it with stdlib `urllib.request`, enforce `--max-bytes`, verify the PNG signature, write to a temporary file, then `os.replace` onto `--output`.
- Credits: read `data.metadata.creditsUsed`, then `data.metadata.creditCount`, then top-level `data.creditsUsed`; default to 0 when all are absent (the same defensive pattern `extract` uses).
- Metadata: pass `data.metadata` through into the status object when it is a dict.
- Payload size is the byte length of the file actually written (`payload_bytes`).

### Status object (scrape)

Stdout carries exactly one object:

```json
{
  "command": "scrape",
  "status": "ok",
  "output_mode": "file",
  "output_path": "/abs/or/given/path",
  "payload_bytes": 182344,
  "summary": {
    "url": "https://example.com",
    "format": "rawHtml",
    "credits_used": 1
  },
  "payload_schema": {
    "command": "string",
    "status": "string",
    "output_mode": "string",
    "output_path": "string",
    "payload_bytes": "number",
    "summary": {"url": "string", "format": "string", "credits_used": "number"}
  }
}
```

`output_path` is reported exactly as given by the user; it is not rewritten to an absolute path. On errors, the existing one-line JSON error object goes to stderr (`{"command":"scrape","error":...,"http_status":...}`) and stdout stays clean.

## Validation rules

1. `--include-domain` and `--exclude-domain` are mutually exclusive (search).
2. `--time-range` and `--start-date`/`--end-date` are mutually exclusive (search).
3. `--start-date` and `--end-date` must be paired.
4. `--max-results` in 1–20; `--timeout` > 0 (search and extract).
5. `search` and `extract` reject `--stdout` (exit 2, before any request). For `usage`, `--stdout` and `--output` are mutually exclusive.
6. Extract URL count in 1–20.
7. `--image-descriptions`, `--topic finance` → usage error.
8. Invalid dates → usage error with the offending value.
9. `usage`: `--timeout` > 0; `--history` in 0–100; `--stdout` and `--output` mutually exclusive.
10. `search`: positional `query` and `--query` are mutually exclusive; exactly one form is required.
11. `search` batch (`--query` present): `--concurrency` ≥ 1; `--output` is a usage error. `--stdout` is a usage error for every search, including batch.
12. `search`: empty or whitespace-only queries are usage errors, in both single and batch mode.
13. `scrape`: exactly one of `--raw-html`, `--html`, `--screenshot` is required; none or more than one is a usage error (exit 2).
14. `scrape`: `--output` is required. Its parent directory must already exist; `scrape` never creates it.
15. `scrape`: `--full-page` and `--viewport` are valid only with `--screenshot`. Viewport is `WIDTHxHEIGHT` with both values positive integers.
16. `scrape`: `--include-tags` and `--exclude-tags` are valid only with `--html`.
17. `scrape`: `--max-age` ≥ 0; `--max-bytes` ≥ 1; `--timeout` > 0.
18. `scrape`: every `--header` must be `Key: Value`; split on the first colon, trim both sides, and reject an empty key or value.
19. `scrape`: `--stdout` is rejected (exit 2, before any request, no file, no credits).
20. `extract`: `--links` and `--only-main-content` are booleans and add no new validation; they leave every existing extract rule in place.

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

`scrape` reuses the same table and adds two local conditions without adding codes: an output larger than `--max-bytes` maps to 12 (payload rejected, no partial file), and a failed screenshot download maps to 13 (network/server class, no partial file).

Errors print a one-line JSON `{"command": ..., "error": ..., "http_status": ...}` to stderr and exit with the mapped code (stdout stays clean).

## Key management

Resolution order: `FIRECRAWL_API_KEY` → `ONEPASSWORD_FIRECRAWL_REFERENCE` (via `op read <ref>`, 10 s timeout, stderr hint on failure) → exit 10 with instructions. The `.env` loader runs before key resolution.

## Output files

Auto-named under `FIRECRAWL_CLI_OUTPUT_DIR` (default `./tmp/firecrawl/`): `{command}_{YYYYMMDD_HHMMSS}_{slug}.json`, slug from the query (search) or first URL (extract), same slug rules as tavily-skill. `--output` writes to the given path. Stdout status object: `{command, status: "ok", output_mode: "file", output_path, payload_bytes, summary: {result_count, failed_count, image_count, credits_used}, payload_schema}` plus a `Saved JSON to ...` line on stderr.

Batch mode keeps the same file naming but emits one file per query (one shared timestamp, index suffix on slug collision) and a batch status object instead: `{command, status: ok|partial|error, output_mode: "batch", output_dir, input: {queries, concurrency, serial, ...shared search flags}, summary: {query_count, success_count, failed_count, credits_used}, results: [{query, output_path, summary, error}]}`. Failed queries carry `output_path: null` and an `error` object.

`scrape` is the deliberate exception to the corpus behavior: it never resolves `FIRECRAWL_CLI_OUTPUT_DIR`, never auto-names a file, and writes only the path given by the required `--output`. Its status object uses the same `{command, status, output_mode: "file", output_path, payload_bytes, summary, payload_schema}` shape, with `summary = {url, format, credits_used}` and `output_path` echoed exactly as supplied. A failed `scrape` writes no file and keeps stdout clean.

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
- **Offline — `scrape` matrix**: parser defaults (format flags false, `--output` unset, default `--max-bytes`, `--timeout` 60); format mutual exclusion (none selected, two selected → exit 2); missing `--output` → exit 2; `--full-page`/`--viewport` with `--html`/`--raw-html` → exit 2; malformed viewport → exit 2; `--include-tags`/`--exclude-tags` with `--raw-html`/`--screenshot` → exit 2; header parsing (valid `Key: Value`, missing colon, empty key, empty value → exit 2); request bodies for `rawHtml`, `html` with tag filters plus `maxAge` and headers, and `screenshot` with `fullPage`/`viewport`, including the seconds → milliseconds timeout conversion; response normalization for all three formats; a mocked screenshot download with PNG signature validation; a failed download → exit 13; an oversized download or file → exit 12 with no partial file left behind.
- **Offline — `extract` additions**: `--links` adds `"links"` to `formats` and normalizes `data.links` (present as an empty list when absent, still present with `--query`); `--only-main-content` sets `onlyMainContent: true`; without either flag the request body and per-URL result shape are unchanged.
- **Live (`tests/test_integration.py`, opt-in via `RUN_FIRECRAWL_INTEGRATION=1`)**: 3–5 real calls (plain search, search + content, `--time-range`, extract with `--query`), asserting `success`, `credits_used` present, `raw_content` non-empty. Consumes a small number of credits. The `usage` live test adds one read-only billing call (0 credits) asserting `remaining_credits`/`plan_credits` are ints and `raw.credit_usage` agrees with the normalized fields; it skips when no key resolves.
- **Live — `scrape` (opt-in)**: 2–3 additional tests — `--raw-html` against `https://example.com`, `--html` with `--include-tags` against the same page, and `extract --links`; assert the output file exists, is non-empty, and the status object reports `payload_bytes`. A live screenshot test is optional because it spends credits; it stays opt-in and skips when no key resolves.

## Open items to verify against live responses during implementation

- Exact `/v2/scrape` response schema and where `creditsUsed`/`creditCount` live.
- Favicon field name in `metadata`.
- Whether `--topic news` + content scraping behaves identically to web (schema indicates `markdown` on news items; confirm).
- `scrape` `rawHtml` output: confirm `data.rawHtml` is the raw field name and that `data.html` is the cleaned variant in live responses.
- `scrape` screenshot request shape: confirm whether `formats: ["screenshot"]` takes a sibling screenshot-options object or an inline object, and confirm the response URL field name (`data.screenshot`).
- Screenshot URL lifetime (the design records a 24-hour expiry) and whether the download response exposes a usable `Content-Type` in addition to the PNG signature check.
- Whether `onlyMainContent` ever changes the `links` format upstream. The design assumes it does not; `extract --only-main-content --links` is the live check.

Resolved during the `usage` work (2026-10-03):

- Credit-usage path confirmed as `GET /v2/team/credit-usage`; `/v2/credit-usage`, `/v2/billing/usage` and `/v2/usage` are all 404 (D5).
- `POST /v2/search` carries no credit-balance fields in body or headers — only `creditsUsed` for the call itself. Response headers are `access-control-allow-origin`, `content-length`, `content-type`, `date`, `etag`, `x-response-time`; there is no `x-ratelimit-*` or remaining-credits header. The billing endpoint is therefore the only source for the balance, and it is not rate-limited for reads.

# PRD — firecrawl-skill

Status: accepted (2026-09-29). Working language: English.

## Problem

Local agent workflows (news aggregation, research sessions, sub-agent pipelines) currently run web search and page-content retrieval through tavily-skill, which calls the Tavily API. Tavily was acquired by Nebius (announced 2026-02-10), and the web access layer for this workspace is being switched to Firecrawl, which the user has purchased.

The replacement must not force any change in downstream consumers: they call `python -m <skill> search|extract` and read the JSON payload files (specifically `data.results[*].raw_content`).

## Users

1. Agent workflows that invoke the CLI and parse the JSON envelope (primary).
2. Humans auditing payloads and the timestamped corpus in the output directory (secondary).

## Requirements (MVP)

- **R1 — search subcommand.** Mirror tavily-skill: positional `query`; `--max-results` (1–20, default 6); `--raw-content` (`off`/`markdown`/`text`, default `markdown`); `--time-range` (`day`/`week`/`month`/`year`); `--start-date`/`--end-date` (`YYYY-MM-DD`, mutually exclusive with `--time-range`, must be paired); `--include-domain`/`--exclude-domain` (repeatable); `--topic` (`general`/`news`/`finance`); `--images`/`--no-images`; `--country`; `--timeout` (seconds, default 60); `--output`. `--stdout` is rejected (exit 2, before any request). A successful search writes the full payload to a file and prints a status object with `output_path`.
- **R2 — extract subcommand.** Mirror tavily-skill: positional `urls` (1–20); `--format` (`markdown`/`text`, default `markdown`); `--query` (relevant-content selection); `--images`/`--no-images`; `--timeout`; `--output`. `--stdout` is rejected (exit 2, before any request). A successful extract writes the full payload to a file and prints a status object with `output_path`.
- **R3 — output contract.** Same envelope shape as tavily-skill: `{command, input, data}`. Search results carry `raw_content` (markdown) when content was requested. Successful `search` and `extract` write the full payload to an auto-named file under `FIRECRAWL_CLI_OUTPUT_DIR` (default `./tmp/firecrawl/`) or to `--output PATH`, and print a lightweight status object to stdout. Passing `--stdout` to those commands exits 2 before any API request, file write, or credit spend. `usage --stdout` remains valid and prints the full usage payload. `credits_used` (from the Firecrawl `creditsUsed` response field) is recorded in `data`.
- **R4 — key management.** `FIRECRAWL_API_KEY` takes priority; otherwise `ONEPASSWORD_FIRECRAWL_REFERENCE` (an `op read`-compatible reference) is resolved via the 1Password CLI. Missing key → exit 10 with a clear message.
- **R5 — exit codes.** 0 ok / 2 usage / 10 auth (401/403) / 11 quota-rate (402/429) / 12 rejected-no-data (other 4xx) / 13 network-server (408/5xx/timeout). No new codes without updating README, skill doc, and tests together.
- **R6 — offline-testable.** Unit tests must run with no network and no key.
- **R7 — cost visibility.** Estimated credits are printed to stderr before the request when content scraping is enabled; actual `credits_used` lands in the payload.
- **R8 — usage subcommand.** `usage` reports account credit balance from `GET /v2/team/credit-usage` (verified live, 2026-10-03): `data = {provider, remaining_credits, plan_credits, credits_used_in_period, billing_period_start, billing_period_end, periods, raw}`. Normalized top-level fields exist only where the API returns values; `raw` keeps the upstream body verbatim. `--history N` (1–100) adds closed billing periods from `GET /v2/team/credit-usage/historical`; `0` (default) is current period only. The call is read-only and consumes 0 credits, so no estimate is printed to stderr (R7 does not apply). Reuses the `{command, input, data}` envelope, the `--stdout`/`--output` semantics, and the R5 exit-code contract unchanged — no new codes.
- **R9 — multi-query batch search.** `search` accepts a repeatable `--query` (in addition to the optional positional `query`) so an agent can launch many independent queries in one process instead of N CLI invocations. Batch queries execute in parallel via the standard-library `ThreadPoolExecutor` (`--concurrency N`, default 4; `--serial` forces 1 and overrides `--concurrency`). Each query still writes its own full-payload file (one-query-one-file corpus invariant preserved); stdout prints exactly one lightweight batch status object with one entry per query (`query`, `output_path`, `summary`, `error`) plus aggregate counts and summed credits. `--stdout` is rejected for every search, including batch. `--output` is single-mode only (usage error in batch). Exit codes reuse R5: 0 on success including partial failure (per-query errors in the status plus a stderr warning, like `extract`), all-fail returns the first failure's mapped code, argument errors stay 2.
- **R10 — scrape subcommand.** `scrape` captures exactly one URL (one positional `URL`; no batch input) in exactly one heavy format, selected by exactly one of these mutually exclusive flags: `--raw-html` (upstream `rawHtml`, unmodified), `--html` (cleaned `html`), or `--screenshot`. `--output PATH` is required for every format; its parent directory must already exist, be writable, and the target must not be an existing directory — these are checked before any request, and `scrape` never creates directories. `scrape` does not write to `FIRECRAWL_CLI_OUTPUT_DIR`, does not auto-name a file, and does not add an entry to the timestamped snapshot corpus — the only file it writes is `--output`. It prints exactly one JSON status object to stdout (`command`, `status`, `output_mode: "file"`, `output_path` exactly as given, `payload_bytes`, `metadata` as an object or `null`, `summary: {url, format, credits_used}`, `payload_schema`), so the envelope invariant holds; `--stdout` is rejected (exit 2) before any request. Screenshot responses carry a temporary URL, so the CLI downloads the image to `--output`, verifies the PNG signature, and writes atomically; a failed download exits 13 and leaves no partial file. `--include-tags`/`--exclude-tags` (repeatable CSS selectors) are valid only with `--html`; `--full-page` and `--viewport WxH` (positive integers) are valid only with `--screenshot`. `--max-age MS` (non-negative integer), repeatable `--header 'Key: Value'` (split on the first colon, key and value non-empty, duplicate keys rejected case-insensitively, never logged or persisted), `--max-bytes N` (default 5 MiB for `--html`/`--raw-html` and 20 MiB for `--screenshot`; exceeding it exits 12 instead of truncating), and `--timeout` (seconds, default 60) complete the surface. No actions, no LLM formats, no batch, no `--query`, `--images`, `--favicon`, or `--formats`. Exit codes stay the R5 contract with no new codes.
- **R11 — extract additions.** Two additive flags, with no other `extract` behavior change. `--links` adds `"links"` to the upstream `formats` request and projects `data.links` into each per-URL result as `links` (a list; an empty list when upstream omits it). The field is present only when `--links` is set, which keeps default payloads small, and it remains present when `--links` is combined with `--query`. `--only-main-content` explicitly sends `onlyMainContent: true`; because that is already Firecrawl's default, it does not change results and does not filter `links`. `--no-only-main-content` sends `onlyMainContent: false` to request the full page. The two flags are mutually exclusive; omitting both leaves the request body unchanged. Existing result fields, `failed_results`, the status object, and the exit codes are unchanged.

## Non-goals (MVP)

- `crawl` / `map` / `interact` / `agent` subcommands
- `/v2/batch/scrape` asynchronous job polling, including as a `scrape` input path
- `scrape` actions, LLM formats (`json` / `summary` / `question`), `/extract`, and multi-format scrape payloads (`--formats`); `scrape` has no stdout payload mode and never writes the snapshot corpus or `FIRECRAWL_CLI_OUTPUT_DIR`
- MCP server, self-hosting, Developer/Research Index integration
- Keyless (no-API-key) Firecrawl mode — rate limits are too low for a default workflow

## Capability gaps (accepted, handled explicitly)

| tavily-skill capability | Firecrawl status | CLI behavior |
|---|---|---|
| Search depth tiers (`basic`/`advanced`/`fast`/`ultra-fast`) | Single search mode | Flag accepted, ignored, warning on stderr, recorded in `input` |
| Image descriptions (`--image-descriptions`) | Not available | Usage error (explicit, not silent) |
| `--topic finance` | No vertical | Usage error; suggest `--include-domain` for finance sites |
| Extract `--query` + `--chunks-per-source` | No chunking; has `highlights` format | `--query` maps to the `highlights` format; `--chunks-per-source` accepted, warned, ignored |
| Domain include **and** exclude together | Mutually exclusive upstream | Usage error |

## Success criteria

1. Offline unit tests green (parser, validation, request building, normalization).
2. Live integration tests green with a real key.
3. A/B acceptance: 10 real historical queries run on both tools; Firecrawl content coverage at least equal on 7+/10, and median latency within 1.5× of Tavily.
4. Zero changes required in downstream workflows.
5. `scrape` offline coverage is complete: format mutual exclusion and required `--output`, the screenshot-only and html-only flag gates, header parsing, request mapping, screenshot download with PNG signature validation, the `--max-bytes` cap, and atomic writes that leave no partial file.
6. `extract --links`, `--only-main-content`, and `--no-only-main-content` request bodies are covered offline, and default `extract` payloads stay unchanged when none of the flags is used.
7. `scrape` writes only its `--output` file, never touches `FIRECRAWL_CLI_OUTPUT_DIR` or the snapshot corpus, prints exactly one JSON object to stdout, and adds no exit codes.

## Cost model (measured local usage, 2026-09)

Firecrawl: 2 credits per 10 results (rounded up) + 1 credit per scraped page. Local measured usage is ~1,100–1,700 full-content searches per month (payload corpus stats), ≈ 13,600 credits/month — above the Hobby tier (5,000 credits), inside Standard (100,000 credits, $83/mo annual).

`usage` (R8) is the programmatic check for this model: it reads the remaining balance and the plan allotment directly, which is how the monthly burn above is measured without the web dashboard.

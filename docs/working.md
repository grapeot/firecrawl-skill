# Working log

## Changelog

- 2026-09-29 — Repository scaffolded (PRD, RFC, AGENTS, README, packaging, skill doc stub). CLI implementation pending.
- 2026-09-29 — API key verified against `POST /v2/search` (HTTP 200, `creditsUsed: 2`).
- 2026-09-29 — CLI implemented (search/extract, stdlib urllib, tavily-skill envelope), 4 recorded fixtures, 74 offline unit tests + 5 opt-in integration tests, all green. Merged via PR #1.
- 2026-09-29 — CI added (GitHub Actions, Python 3.9/3.12 matrix, setup-uv, offline pytest).
- 2026-10-03 — `usage` subcommand added (R8/D5): `GET /v2/team/credit-usage` for the balance, `GET /v2/team/credit-usage/historical` behind `--history N` for closed billing periods. 0-credit read-only path, exit-code contract unchanged. 3 recorded fixtures (`credit_usage_ok`, `credit_usage_historical_ok`, `credit_usage_not_found`), 105 offline unit tests (+29) and 6 opt-in integration tests (+1), all green.
- 2026-10-03 — Hardened `usage` normalization: a truthy non-dict upstream `data` (list/str/number) previously raised `AttributeError` and escaped `main()` as exit 1, violating the exit-code contract; now degrades to null fields (exit 0). Non-dict entries in historical `periods` are skipped rather than crashing. +2 regression tests (110 offline, all green).
- 2026-10-03 — Added `data.cost_breakdown` to `search` and `extract` payloads: per-document reconstruction of the credit charge (`base` = 2×ceil(results/10); HTML 1; x.com/twitter 30; PDF 1/page; unreturned 0), with `modelled_total`/`reported_total`/`reconciles`/`warning`. Best-effort, never blocks. Verified against 212 recorded search + 164 extract calls (2026-10), all reconcile exactly. +23 offline tests (133 total). Host matching handles FQDN trailing dots, case, ports, and rejects look-alikes (evilx.com, x.com.evil.com); bool JSON figures are rejected from billing arithmetic.

## Lessons learned

- The Firecrawl `search` feature docs and the v2 OpenAPI drift apart occasionally (e.g. a `context` parameter mentioned in blog posts is absent from the v2 OpenAPI). Treat the OpenAPI as the contract; verify live responses before depending on undocumented fields.
- `includeDomains`/`excludeDomains` are implemented upstream by injecting `site:` operators into the query — domain filtering is query-level, not server-side ranking.
- `POST /v2/scrape` returns no top-level `creditsUsed`; per-page credits live in `data.metadata.creditsUsed` (a `creditCount` alias appears in some responses). The CLI checks both plus the top level defensively.
- The `highlights` format returns a plain string, not a list, even though it can contain multiple highlight sections. Normalize to a list at the envelope boundary.
- Firecrawl markdown for some pages (e.g. example.com) contains no heading lines — page titles live in `data.metadata.title`, not in the markdown body.
- Firecrawl's *docs page slug* and its *API path* differ for billing: the page is `/api-reference/endpoint/credit-usage`, the endpoint is `/v2/team/credit-usage`. Guessing `/v2/credit-usage` (or `/v2/usage`, `/v2/billing/usage`) returns 404 with `code: "NOT_FOUND"` and an `error` string naming the path that was tried. Trust the published `api-reference/v2-openapi.json` paths, not the URL slug.
- The historical credit endpoint's OpenAPI schema names the per-period value `totalCredits`; the live response names it `creditsUsed`. Read both, first-present wins. This is the same class of drift as `creditsUsed`/`creditCount` on `/v2/scrape` noted above.
- `/v2/search` exposes no balance anywhere: no remaining-credits response header (no `x-ratelimit-*` at all), and the body carries only `creditsUsed` for the call itself. A balance check must be a separate billing-endpoint call — which is why `usage` exists rather than a field on `search`.
- `planCredits` excludes coupon and pay-as-you-go credits (per the upstream schema description), so `plan_credits - remaining_credits` is a floor for spend, not an exact ledger; that is why the derived field is named `credits_used_in_period` and `raw` is kept in the payload.
- Firecrawl returns no plan *name* on any billing endpoint, so the CLI reports no plan name. `usage` is stdlib GET + a 165-byte body, so it costs 0 credits; there is no `Estimated Firecrawl credits:` line to print for it.

# Working log

## Changelog

- 2026-09-29 — Repository scaffolded (PRD, RFC, AGENTS, README, packaging, skill doc stub). CLI implementation pending.
- 2026-09-29 — API key verified against `POST /v2/search` (HTTP 200, `creditsUsed: 2`).
- 2026-09-29 — CLI implemented (search/extract, stdlib urllib, tavily-skill envelope), 4 recorded fixtures, 74 offline unit tests + 5 opt-in integration tests, all green. Merged via PR #1.
- 2026-09-29 — CI added (GitHub Actions, Python 3.9/3.12 matrix, setup-uv, offline pytest).

## Lessons learned

- The Firecrawl `search` feature docs and the v2 OpenAPI drift apart occasionally (e.g. a `context` parameter mentioned in blog posts is absent from the v2 OpenAPI). Treat the OpenAPI as the contract; verify live responses before depending on undocumented fields.
- `includeDomains`/`excludeDomains` are implemented upstream by injecting `site:` operators into the query — domain filtering is query-level, not server-side ranking.
- `POST /v2/scrape` returns no top-level `creditsUsed`; per-page credits live in `data.metadata.creditsUsed` (a `creditCount` alias appears in some responses). The CLI checks both plus the top level defensively.
- The `highlights` format returns a plain string, not a list, even though it can contain multiple highlight sections. Normalize to a list at the envelope boundary.
- Firecrawl markdown for some pages (e.g. example.com) contains no heading lines — page titles live in `data.metadata.title`, not in the markdown body.

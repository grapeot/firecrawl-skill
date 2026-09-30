# Working log

## Changelog

- 2026-09-29 — Repository scaffolded (PRD, RFC, AGENTS, README, packaging, skill doc stub). CLI implementation pending.
- 2026-09-29 — API key verified against `POST /v2/search` (HTTP 200, `creditsUsed: 2`).

## Lessons learned

- The Firecrawl `search` feature docs and the v2 OpenAPI drift apart occasionally (e.g. a `context` parameter mentioned in blog posts is absent from the v2 OpenAPI). Treat the OpenAPI as the contract; verify live responses before depending on undocumented fields.
- `includeDomains`/`excludeDomains` are implemented upstream by injecting `site:` operators into the query — domain filtering is query-level, not server-side ranking.

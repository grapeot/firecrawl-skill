# Firecrawl Skill

Real-time web search, URL content extraction, single-page capture, and account credit-usage reporting through the Firecrawl v2 API. `search` and `extract` always save the full JSON payload to a file. Stdout returns a lightweight status object containing `output_path`, which you open to inspect the complete results. `scrape` writes the captured HTML or image bytes to the required `--output` path and prints a status object; it deliberately does not write to the snapshot corpus.

The `search` and `extract` command surface and file-output envelope mirror tavily-skill, except that those two commands do not accept `--stdout`. `usage` is a firecrawl-skill addition with its own `data` block (Firecrawl has no credits-usage equivalent in tavily). `scrape` has no tavily counterpart; it is the single-page structural capture primitive and is specified in `docs/rfc.md` (D9).

## When to use

Trigger when the user expresses any of these intents:

- Look up the latest information, news, or web content
- Search a topic and keep structured JSON results
- Need to limit result count, time range, or domain scope
- Need image results (no LLM image descriptions — Firecrawl does not provide them)
- Already have a URL and want to extract its body content directly
- Need a page's unmodified raw HTML, its cleaned HTML, or a screenshot — one page, one structural format (`scrape`)
- Any scenario suited for real-time web search via Firecrawl
- Ask how many Firecrawl credits are left, how much was used this billing period, or whether a budget/plan threshold has been crossed (`usage`)

## Which command

| Need | Command | Why |
|---|---|---|
| Find pages for a topic | `search` | Discovery; returns result URLs, snippets, and optional markdown |
| Batch content from 1–20 known URLs | `extract` | Markdown by default, optional highlights, images, and links; payload joins the timestamped corpus |
| One page's structure (raw HTML, cleaned HTML, screenshot) | `scrape` | Exactly one format, one URL, required `--output`; no corpus write |
| Credits or monthly burn | `usage` | Read-only billing endpoint, 0 credits |

In short: `extract` is the batch content command; `scrape` is the single-page structural capture command. If you only need readable text from one URL and want the payload archived alongside other research, use `extract`. Reach for `scrape` when the artifact itself matters (HTML structure, selectors, a rendered screenshot) or when the output must land at an exact path.

## Prerequisites

- Entry point: `python -m firecrawl_skill` (after installing the package from this repository root)
- Python dependencies: none (standard library only); dev extras via `pip install -e ".[dev]"`
- API key: `FIRECRAWL_API_KEY` takes priority; optionally `ONEPASSWORD_FIRECRAWL_REFERENCE` (value is an `op read`-compatible reference; never commit private vault paths to a public repository)

## First-time setup

On the first use in a workspace, check whether `FIRECRAWL_CLI_OUTPUT_DIR` is set (in the repo's `.env` or the ambient environment). If it is not set, mention this to the user once during setup — not at runtime — and recommend pointing it at one dedicated, persistent directory (for example a knowledge-base `web_snapshots/firecrawl_raw/` folder). The default `./tmp/firecrawl/` is ephemeral and resolves relative to the current working directory, so payloads scatter across session directories and get cleaned up; a single stable directory lets every search/extract payload accumulate as a timestamped corpus of primary sources, which pays off over time for research, auditing, and later retrieval. This setting applies to `search` and `extract` only: `scrape` always writes to its required `--output` path and ignores the output directory.

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

### Reading results from disk

`search` and `extract` do not print full page bodies or complete payloads to stdout. The `summary` and `payload_schema` objects are high-level counters and structural hints, not the full payload content.

Run this reader from the repository root with the environment active. It checks the exit code before parsing stdout, preserves stderr (including partial-success warnings), and opens the exact returned paths. Relative paths resolve against the CLI's working directory (`cli_cwd`); do not select files by directory order, modification time, or globbing. In batch mode, failed queries have null paths and are skipped; their errors remain in the status object and stderr.

The reader displays at most 3 results across the entire batch, with query/title/url limited to 120/160/240 characters and each body excerpt limited to 500 characters. Total serialized preview output, including JSON escaping and newlines, is limited to 4,000 characters; stderr diagnostics are preserved separately. Preview fields are locally derived, not a new payload schema. Full bodies remain intact on disk. A prefix can miss relevant text deeper in a page; use the indexed range reader below to expand a selected passage.

```bash
python - <<'PY'
import json
from pathlib import Path
import subprocess
import sys

cli_cwd = Path.cwd()
cmd = [sys.executable, "-m", "firecrawl_skill", "search", "latest AI news"]
# Replace cmd for extract or batch search; neither accepts --stdout:
# cmd = [sys.executable, "-m", "firecrawl_skill", "extract", "https://example.com"]
# cmd = [sys.executable, "-m", "firecrawl_skill", "search", "--query", "AI news", "--query", "LLM releases"]
# For a named single-search file, append: ["--output", "custom_results.json"]
proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cli_cwd)

# Check failure BEFORE parsing stdout: errors are not result payloads.
if proc.returncode != 0:
    sys.stderr.write(proc.stderr)
    sys.exit(proc.returncode)
sys.stderr.write(proc.stderr)

status = json.loads(proc.stdout)
if status["output_mode"] == "file":
    paths = [status["output_path"]]
elif status["output_mode"] == "batch":
    paths = [r["output_path"] for r in status["results"]
             if r["error"] is None and r["output_path"] is not None]
else:
    raise ValueError("Expected a file or batch status object")

MAX_RESULTS = 3
BODY_CHARS = 500
MAX_OUTPUT_CHARS = 4000
total_results = 0
total_output_chars = 0
budget_exhausted = False

for p in paths:
    if budget_exhausted or total_results >= MAX_RESULTS:
        break
    json_path = Path(p)
    if not json_path.is_absolute():
        json_path = cli_cwd / json_path
    with json_path.open(encoding="utf-8") as f:
        payload = json.load(f)
    data = payload["data"]
    query = (data.get("query") or payload["input"].get("query") or "")[:120]
    for idx, item in enumerate(data["results"]):
        if total_results >= MAX_RESULTS:
            break
        body = item.get("raw_content" if payload["command"] == "search" else "markdown") or ""
        # Locally derived preview; the exact path and index locate the full body.
        preview = {
            "output_path": str(json_path), "result_index": idx,
            "query": query, "title": (item.get("title") or "")[:160],
            "url": (item.get("url") or "")[:240], "body_chars": len(body),
            "excerpt": body[:BODY_CHARS], "truncated": len(body) > BODY_CHARS,
        }
        line = json.dumps(preview, ensure_ascii=False) + "\n"
        if total_output_chars + len(line) > MAX_OUTPUT_CHARS:
            budget_exhausted = True
            break  # Keep the exact locator and JSON intact; do not truncate the line.
        sys.stdout.write(line)
        total_output_chars += len(line)
        total_results += 1
PY
```

#### Expanding a selected range

Set `payload_path` to the exact returned `output_path` for the query of interest (the path below is a placeholder). For a relative path, keep the CLI's working directory or use the absolute path from a preview. Pass a zero-based result index and character offsets; `end` is exclusive. This reader prints only the requested slice, at most 800 characters plus a newline, without calling the CLI or changing the saved JSON.

```bash
payload_path="tmp/firecrawl/search_results.json"  # Replace with the exact output_path.
python - "$payload_path" 0 500 1000 <<'PY'
import json
from pathlib import Path
import sys

payload_path = Path(sys.argv[1])
result_index = int(sys.argv[2])
start, end = int(sys.argv[3]), int(sys.argv[4])
if not (0 <= start < end and end - start <= 800):
    raise ValueError("Require 0 <= start < end and end - start <= 800")
if not payload_path.is_absolute():
    payload_path = Path.cwd() / payload_path
with payload_path.open(encoding="utf-8") as f:
    payload = json.load(f)
results = payload["data"]["results"]
if not (0 <= result_index < len(results)):
    raise IndexError("result_index is outside data.results")
item = results[result_index]
body = item.get("raw_content" if payload["command"] == "search" else "markdown") or ""
print(body[start:end])
PY
```

### Many queries in one call (batch mode)

When you need several independent queries, launch them in a single process instead of N CLI invocations — the queries run in parallel internally, so the model makes one round-trip. Each `--query` is one complete query string; multi-word queries are not split.

```bash
python -m firecrawl_skill search \
  --query "latest AI news" \
  --query "openai releases" \
  --query "anthropic news"
```

Batch mode (one or more `--query`) runs with `ThreadPoolExecutor`, `--concurrency N` workers (default 4) or sequentially with `--serial` (`--serial` overrides `--concurrency` and equals `--concurrency 1`). The positional query and `--query` are mutually exclusive; both or neither is a usage error (exit 2). Batch search rejects `--output` because each query writes to its own file. Neither single-query nor batch `search` accepts `--stdout`.

Batch mode preserves the one-query-one-file corpus invariant: each query writes its own full payload to an auto-named `search_{timestamp}_{slug}.json` under `FIRECRAWL_CLI_OUTPUT_DIR` (or `./tmp/firecrawl/`), with an index suffix if two queries slugify to the same name. Stdout returns exactly one batch status object, not the result content:

```json
{
  "command": "search",
  "status": "ok",
  "output_mode": "batch",
  "output_dir": "/path/to/output",
  "input": { "queries": ["q1", "q2"], "concurrency": 4, "serial": false },
  "summary": { "query_count": 2, "success_count": 2, "failed_count": 0, "credits_used": 16 },
  "results": [
    { "query": "q1", "output_path": "/path/to/output/search_..._q1.json", "summary": { "result_count": 6, "credits_used": 8 }, "error": null },
    { "query": "q2", "output_path": "/path/to/output/search_..._q2.json", "summary": { "result_count": 6, "credits_used": 8 }, "error": null }
  ],
  "payload_schema": { "...": "schema hint for this envelope" }
}
```

`status` is `ok` (all queries succeed), `partial` (some fail), or `error` (all fail). A partial failure exits 0 with the failed queries marked `error` in the status and reported on stderr, matching `extract`'s partial-failure semantics; only an all-fail batch exits with the first failure's mapped code (10/11/12/13). Batch `credits_used` sums the successful queries only. Read the per-query files under `results[*].output_path` for the actual results.

### URL content extraction

```bash
python -m firecrawl_skill extract https://firecrawl.dev
python -m firecrawl_skill extract https://firecrawl.dev --query "agent search" --output /tmp/firecrawl_extract.json
```

`--query` requests query-relevant highlights for each page (Firecrawl returns the relevant passages; there is no chunk-count control).

`--links` adds a `links` array to every successful per-URL result. It is opt-in so default payloads stay small, it remains present when combined with `--query`, and it is an empty list when upstream returns none. `--only-main-content` explicitly sends `onlyMainContent: true`; because that is already Firecrawl's default, it does not change results and does not filter `links`. `--no-only-main-content` sends `onlyMainContent: false` to request the full page. The two flags are mutually exclusive, and omitting both leaves the request body unchanged.

```bash
python -m firecrawl_skill extract https://example.com --links
python -m firecrawl_skill extract https://example.com --links --only-main-content --output /tmp/site.json
python -m firecrawl_skill extract https://example.com --links --no-only-main-content
```

### Single-page capture (scrape)

Use `scrape` when you need the page itself in one structural form, not reader-friendly markdown. Exactly one of `--raw-html`, `--html`, or `--screenshot` is required, and `--output` is always required.

```bash
# unmodified HTML
python -m firecrawl_skill scrape https://example.com --raw-html --output /tmp/example.html

# cleaned HTML, restricted to the main element
python -m firecrawl_skill scrape https://example.com --html \
  --include-tags main --exclude-tags nav --output /tmp/example-main.html

# full-page screenshot at a fixed viewport
python -m firecrawl_skill scrape https://example.com --screenshot \
  --full-page --viewport 1440x900 --output /tmp/example.png
```

- `--include-tags` / `--exclude-tags` (repeatable CSS selectors) only work with `--html`. Passing them with `--raw-html` or `--screenshot` is a usage error (exit 2), because filtering would make "raw" misleading and a screenshot has no DOM.
- `--full-page` / `--viewport WxH` only work with `--screenshot`; `WxH` takes positive integers.
- Screenshot responses carry a temporary URL, so the CLI downloads the image, verifies the PNG signature, and writes atomically. A failed download exits 13 and leaves no partial file.
- `--max-bytes N` defaults to 5 MiB for HTML and 20 MiB for screenshots. Exceeding it exits 12 rather than truncating, and no partial file remains.
- `--max-age MS` (non-negative milliseconds) and repeatable `--header 'Key: Value'` are supported. Header values are never logged or persisted.
- `scrape` never writes `FIRECRAWL_CLI_OUTPUT_DIR`, never auto-names a file, and never adds a corpus entry. It writes only `--output`, whose parent must already exist and be writable; the target must not be an existing directory. These checks run before any request.
- There are no actions, no LLM formats (`json`/`summary`/`question`), no `/extract`, and no batch input.

### Disable images or raw content

```bash
python -m firecrawl_skill search "earnings news" --no-images --raw-content off
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
- Default mode writes the full JSON payload to an auto-named file under `tmp/firecrawl/` (or `FIRECRAWL_CLI_OUTPUT_DIR`) and prints a lightweight status object to stdout; hints, including an estimated credit cost, go to stderr
- `--output PATH` writes the full JSON payload to that file and prints a lightweight status object to stdout. Supported for single-query `search`, `extract`, and `usage` (rejected only for batch search). For `usage`, `--output` and `--stdout` are mutually exclusive
- `--stdout` is rejected on `search`, `extract`, and `scrape` (exit 2, before any request, no credits spent). `usage` still accepts it and prints the complete usage payload
- `scrape` requires exactly one of `--raw-html`/`--html`/`--screenshot` and a `--output` path. `--max-bytes` defaults to 5 MiB (HTML) or 20 MiB (screenshot); it never writes the corpus
- `credits_used` in `data` records the actual Firecrawl credits consumed (from the API response)
- `data.cost_breakdown` explains that number per document (HTML 1, x.com/twitter 30, PDF 1/page, unreturned 0); best-effort, non-blocking, with a `warning` on mismatch
- `usage` is the exception to the estimates above: it costs 0 credits and prints no `Estimated Firecrawl credits:` line

## Parameter reference

### `search`

| Parameter | Description | Default |
|---|---|---|
| `query` | Positional search query (single-query mode; mutually exclusive with `--query`) | required unless `--query` is used |
| `--query` | One complete query string; repeat for a parallel multi-query batch (mutually exclusive with the positional query) | — |
| `--concurrency` | Batch mode: number of queries in flight at once (≥1) | `4` |
| `--serial` | Batch mode: force sequential execution; overrides `--concurrency` (equals `--concurrency 1`) | `False` |
| `--max-results` | Number of results, range 1–20 | `6` |
| `--search-depth` | `basic` / `advanced` / `fast` / `ultra-fast` (accepted for compatibility; Firecrawl has a single search mode, so the value is ignored with a stderr warning) | `advanced` |
| `--topic` | `general` / `news` (`finance` is rejected; use `--include-domain` on finance sites) | `general` |
| `--time-range` | `day` / `week` / `month` / `year` | — |
| `--start-date` | Start date, `YYYY-MM-DD` (paired with `--end-date`) | — |
| `--end-date` | End date, `YYYY-MM-DD` (paired with `--start-date`) | — |
| `--include-domain` | Restrict to a domain; repeat for multiple (mutually exclusive with `--exclude-domain`) | — |
| `--exclude-domain` | Exclude a domain; repeat for multiple (mutually exclusive with `--include-domain`) | — |
| `--stdout` | Rejected for `search`. Exits 2 before any request. The full payload is always written to a file | — |
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
| `--links` | Add a `links` array to every successful per-URL result (present only when set) | `False` |
| `--only-main-content` | Explicitly send `onlyMainContent: true`; matches the upstream default, so results are unchanged; does not filter `links` | unset (upstream `true`) |
| `--no-only-main-content` | Send `onlyMainContent: false` to request the full page; mutually exclusive with `--only-main-content` | `False` |
| `--extract-depth` | `basic` / `advanced` (accepted for compatibility; always ignored with a warning) | `advanced` |
| `--chunks-per-source` | Accepted for compatibility; always ignored with a warning | — |
| `--images` | Enable image extraction | `False` |
| `--no-images` | Disable image extraction | `False` |
| `--favicon` | Pass favicon URL through from metadata when present | `False` |
| `--timeout` | Per-URL request timeout in seconds | `60` |
| `--stdout` | Rejected for `extract`. Exits 2 before any request. The full payload is always written to a file | — |
| `--output` | Write full extract payload to a named JSON file; stdout still returns status schema | auto-writes to `tmp/firecrawl/` or `FIRECRAWL_CLI_OUTPUT_DIR` |

### `scrape`

| Parameter | Description | Default |
|---|---|---|
| `URL` | Exactly one URL (no batch input) | required |
| `--raw-html` | Capture unmodified HTML (`formats: ["rawHtml"]`); mutually exclusive with the other format flags | `False` |
| `--html` | Capture cleaned HTML (`formats: ["html"]`); the only format that accepts tag filters | `False` |
| `--screenshot` | Capture a screenshot; the response URL is downloaded and PNG-verified | `False` |
| `--output` | Destination file for the captured bytes; required, parent must exist, never auto-named | required |
| `--include-tags` | CSS selector to include; repeatable; only with `--html` | — |
| `--exclude-tags` | CSS selector to exclude; repeatable; only with `--html` | — |
| `--full-page` | Capture the full page; only with `--screenshot` | `False` |
| `--viewport` | `WIDTHxHEIGHT` with positive integers; only with `--screenshot` | — |
| `--max-age` | Cached-result age limit in milliseconds (≥0) | — |
| `--header` | Extra request header as `Key: Value`; repeatable; never logged or persisted | — |
| `--max-bytes` | Local size cap; exceeding it exits 12 with no partial file | `5 MiB` HTML, `20 MiB` screenshot |
| `--timeout` | Request timeout in seconds | `60` |
| `--stdout` | Rejected for `scrape`. Exits 2 before any request; captured bytes always go to `--output` | — |

Exactly one of `--raw-html`, `--html`, `--screenshot` is required. There is no `--formats`, no batch input, no actions, and no LLM formats.

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

`search`'s `data.results` retains one item per web result with `url`, `title`, `description`, `position`, `raw_content` (markdown when requested, `null` otherwise), optional `links` and `metadata`. News items also land in `data.news`; with `--topic news` they are merged into `data.results` as well. For `extract`, `data.results` holds one item per URL (`markdown`, optional `highlights`/`images`/`links`/`metadata`) and additionally carries `failed_results` and `failed_count`. `links` appears on an extract result only when `--links` was requested; the default per-URL shape is unchanged.

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

`scrape` has no JSON payload of its own: `--output` contains the captured HTML or PNG bytes. Its status object names the artifact instead of describing a result set:

```json
{
  "command": "scrape",
  "status": "ok",
  "output_mode": "file",
  "output_path": "/tmp/example.html",
  "payload_bytes": 182344,
  "metadata": { "title": "Example Domain" },
  "summary": { "url": "https://example.com", "format": "rawHtml", "credits_used": 1 },
  "payload_schema": {
    "command": "string",
    "status": "string",
    "output_mode": "string",
    "output_path": "string",
    "payload_bytes": "number",
    "metadata": "object|null",
    "summary": { "url": "string", "format": "string", "credits_used": "number" }
  }
}
```

`output_path` is echoed exactly as given. Read that file directly; do not parse it as JSON, because its contents are HTML or a PNG.

The full payload prints to stdout only when running `usage --stdout`. `search` and `extract` never print full payloads to stdout and reject `--stdout`; they always write the complete JSON payload to disk and emit a lightweight status object (`output_path`, summary including `credits_used`, and `payload_schema`). `scrape` likewise rejects `--stdout`, but its file is the captured artifact rather than a JSON envelope.

## Speed optimization

For independent queries, prefer one batch invocation over N separate CLI calls:

```bash
python -m firecrawl_skill search --query "q1" --query "q2" --query "q3"
```

Each standalone `python -m firecrawl_skill search "<q>"` invocation re-pays a fixed cost before the first byte of a response: process/interpreter startup, module import, TLS setup, and API-key resolution. When the key comes from `ONEPASSWORD_FIRECRAWL_REFERENCE`, resolution is an `op read` subprocess call and costs roughly 0.9s per invocation on top of the rest. Batch mode pays this once and then collapses the N serial API waits into one parallel wave, so the saving grows with N.

To remove the `op read` term entirely, put a raw `FIRECRAWL_API_KEY=...` directly in the repo-local, gitignored `.env`. The CLI reads `FIRECRAWL_API_KEY` first and skips 1Password, reducing the per-call fixed cost to process startup plus connection setup. Never commit that value or place it in any tracked file.

`benchmarks/latency.py` measures the difference end to end. It is opt-in (`RUN_FIRECRAWL_LATENCY=1`) because it spends real credits; use it to size the win for your own query mix rather than trusting a single number.

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
- The commands are `search`, `extract`, `scrape`, and `usage`. `extract` is the batch content command; `scrape` is the single-page structural capture command
- `scrape` requires exactly one format flag and `--output`; it never writes `FIRECRAWL_CLI_OUTPUT_DIR` or the snapshot corpus, and its `payload_bytes` counts the bytes of the written artifact
- `scrape` uses the same exit codes as every other command: 12 for an oversized payload (`--max-bytes`), 13 for a failed screenshot download; no new codes
- `usage` reads a billing endpoint: 0 credits, and its normalized fields exist only where the API returns values (`data.raw` keeps the upstream body verbatim)
- `--output PATH` is supported for single-query `search`, `extract`, and `usage` (rejected only for batch search). It writes the full JSON payload to the specified file and prints the status object to stdout. For `usage`, `--output` and `--stdout` remain mutually exclusive. When omitted, file outputs are written under `tmp/firecrawl/` or `FIRECRAWL_CLI_OUTPUT_DIR`; `usage --stdout` instead prints the full payload
- `scrape` is the exception: `--output` is required rather than optional, and its parent directory must already exist. The file holds the captured HTML or PNG bytes, not a JSON envelope. `scrape` never uses `tmp/firecrawl/` or `FIRECRAWL_CLI_OUTPUT_DIR`
- Batch search writes one full JSON payload file per query and prints a single status object to stdout. It rejects `--output` and `--stdout`. A partial failure still exits 0
- Exit codes: 0 ok / 2 usage error (bad arguments) / 10 auth / 11 quota-rate / 12 rejected-no-data / 13 network-server — the same table for every command, `usage` and `scrape` included

## Operational guidance

- Run the CLI from this skill repo's root directory using its project-local interpreter: `./.venv/bin/python -m firecrawl_skill ...`. Do not run it from a parent workspace root, because the parent environment may not load this repo's `.env`, so `ONEPASSWORD_FIRECRAWL_REFERENCE` / `FIRECRAWL_API_KEY` may be missing even though sub-agents or project-local calls work.
- To consume results in the current turn, read the file specified by `output_path` in the stdout status object. `search`, `extract`, and `scrape` reject `--stdout`. Search/extract payloads land under `tmp/firecrawl/` (or `FIRECRAWL_CLI_OUTPUT_DIR` when set), unless `--output` names another file. `scrape` writes only its required `--output`; read that file as HTML or an image, not as JSON.
- When the artifact matters, use `scrape` instead of `extract`: raw HTML preserves the DOM for selector work, `--html` with tags trims boilerplate, and a screenshot records the rendered page. The actual charge is reported in `summary.credits_used`.
- For routine research, default to `--raw-content markdown`. Base judgments on `data.results[*].raw_content`, source URLs, page titles, and snippet content.
- Only pass `--raw-content off` when payload size is a confirmed bottleneck (it is also materially cheaper upstream: 2 credits per 10 results instead of 2 + 1 per page).
- Mind the cost model: each full-content search at 6 results costs ~8 Firecrawl credits. Estimate credits are printed to stderr before the request; actual usage lands in `data.credits_used`.
- Check the balance with `python -m firecrawl_skill usage --stdout` (0 credits) before anything that scrapes many pages, and re-check after a large batch instead of adding up `data.credits_used` by hand. `usage --history 6` shows the trailing billing periods when you need a monthly burn rate rather than a snapshot.
- Because there are no implicit retries, an exit 11 (402/429) from `search` means the caller decides what to do next — confirm the balance with `usage` before retrying.

# Latency benchmark

`latency.py` measures how much wall-clock time batch mode saves over the
per-query CLI pattern. It runs the real Firecrawl API through child processes
and compares three execution modes for the same number of independent queries.

## Opt-in

Every run spends real Firecrawl credits. The script is gated behind an
environment variable: with `RUN_FIRECRAWL_LATENCY` unset it prints a skip
notice and exits 0 without any network call, so CI and offline runs stay
credit-free.

```bash
# skip (no credits, exits 0)
python benchmarks/latency.py

# actually measure (spends credits)
RUN_FIRECRAWL_LATENCY=1 python benchmarks/latency.py --queries 4 --repeats 2
```

Run it with the repo's project interpreter from the repository root
(`.venv/bin/python benchmarks/latency.py ...`) so that children inherit the
editable `firecrawl_skill` install and the repo-local `.env`.

## Modes

| Mode | What runs | What it represents |
|---|---|---|
| `standalone` | N separate `python -m firecrawl_skill search "<q>"` processes, one after another | the agent pattern of shelling out once per query |
| `batch_parallel` | one process with N `--query` values (`--concurrency C`, default 4) | one round-trip, queries in flight at once |
| `batch_serial` | one process with N `--query` values plus `--serial` | one process, but the API calls are serialized |

Batch mode is the structural win: the fixed per-process cost (interpreter
startup, importing the CLI, TLS setup, and API-key resolution) is paid once
instead of N times, and the N serial API waits collapse into a single parallel
wave. `standalone` therefore pays both costs N times.

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `--queries N` | `4` | independent queries per measurement |
| `--repeats R` | `3` | repeats per mode; the median is reported |
| `--concurrency C` | `4` | workers for `batch_parallel` |
| `--max-results M` | `3` | results per query (keeps cost and payload small) |
| `--json-out PATH` | none | write the full report as JSON |

## How to read the output

Each mode is run `--repeats` times and one median wall-clock time is reported
per mode (plus min/max and sample count). The headline number is:

```
speedup (standalone total / batch-parallel) = median(standalone) / median(batch_parallel)
```

`standalone` is the total time to run all N queries sequentially, so a speedup
of 3–4x at N=4 means batch mode is roughly hitting the expected parallel
ceiling rather than merely shaving overhead. `batch_serial` is the control: it
isolates the parallelism win from the fixed-overhead win, so
`standalone / batch_serial` approximates the per-process overhead saved and
`batch_serial / batch_parallel` approximates the parallel wave.

## Methodology notes and caveats

- **Unique queries per measurement.** Each measurement appends a short random
  token to the query text, so the provider never serves a cached answer for a
  question it has already seen. Without this, later repeats get warm and the
  comparison is meaningless — this was a real measurement trap.
- **Isolated corpus.** Every child gets `FIRECRAWL_CLI_OUTPUT_DIR` pointed at a
  throwaway temp directory that is deleted afterward, so the real timestamped
  snapshot corpus never fills up with benchmark noise.
- **Network variance.** Absolute seconds depend on latency to the Firecrawl API
  and on provider load. Compare modes within a single run, not across runs;
  raising `--repeats` tightens the median.
- **Cold vs warm.** A process is always cold (fresh interpreter, fresh TLS), so
  the fixed-cost term is honest. The API is not: a query issued moments earlier
  may be served faster. Unique queries neutralize that.
- **Failure handling.** A free `usage` preflight aborts the run if no API key
  resolves. A failed child is counted and surfaced as a stderr warning; its
  timing still enters the median, so nonzero failure counts make the numbers
  less trustworthy.
- **No secrets.** The script never reads `.env` and never prints or persists
  the API key; children resolve it themselves from the repo root or the
  ambient environment.

## Cost

With the defaults (`--queries 4 --repeats 3`), a run issues 4x3 standalone
searches + one 4-query parallel batch x3 + one 4-query serial batch x3, plus
one free `usage` call. At `--max-results 3` each search is cheap, but repeated
runs still add up. Keep `--repeats 2` while sanity-checking.

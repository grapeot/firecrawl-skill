#!/usr/bin/env python3
"""Opt-in latency benchmark for firecrawl-skill.

Compares three ways to run N independent `search` queries through the real
Firecrawl API:

  standalone      N separate `python -m firecrawl_skill search "<q>"` processes
                  (the pattern an agent uses when it shells out per query)
  batch_parallel  one process, N `--query` values, ThreadPoolExecutor (default)
  batch_serial    one process, N `--query` values, `--serial`

The run spends real Firecrawl credits, so it is gated behind
``RUN_FIRECRAWL_LATENCY=1``. When that variable is unset the script prints a
skip notice and exits 0 without touching the network, which keeps CI and
offline runs credit-free.

Standard library only. The script never reads `.env` itself and never prints,
logs, or persists the API key: every child process resolves the key on its own
from the repo-local `.env` (or the ambient environment), exactly like a normal
CLI call. Children are started from the repository root so that `.env` is
found, and each one writes its snapshots into a throwaway output directory
(``FIRECRAWL_CLI_OUTPUT_DIR``) so the real corpus is never polluted.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable, Sequence

OPT_IN_ENV = "RUN_FIRECRAWL_LATENCY"
OUTPUT_DIR_ENV = "FIRECRAWL_CLI_OUTPUT_DIR"

SKIP_MESSAGE = "skipped (set RUN_FIRECRAWL_LATENCY=1; spends real credits)"

MODE_STANDALONE = "standalone"
MODE_BATCH_PARALLEL = "batch_parallel"
MODE_BATCH_SERIAL = "batch_serial"
MODE_ORDER: tuple[str, ...] = (MODE_STANDALONE, MODE_BATCH_PARALLEL, MODE_BATCH_SERIAL)

DEFAULT_QUERIES = 4
DEFAULT_REPEATS = 3
DEFAULT_CONCURRENCY = 4
DEFAULT_MAX_RESULTS = 3
MAX_RESULTS_LIMIT = 20

# Injectable clock so the orchestration can be unit-tested without real time.
_clock: Callable[[], float] = time.perf_counter


# ---------------------------------------------------------------------------
# Pure helpers (offline-testable)
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="latency.py",
        description=(
            "Opt-in latency benchmark comparing standalone search invocations "
            "with one batch-parallel process. Spends real Firecrawl credits; "
            f"gated behind {OPT_IN_ENV}=1."
        ),
    )
    parser.add_argument(
        "--queries",
        type=int,
        default=DEFAULT_QUERIES,
        help=f"Independent queries per measurement (default {DEFAULT_QUERIES}).",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=DEFAULT_REPEATS,
        help=f"Repeats per mode; the median is reported (default {DEFAULT_REPEATS}).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Batch-parallel workers (default {DEFAULT_CONCURRENCY}).",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=DEFAULT_MAX_RESULTS,
        help=(
            f"Results per query, {1}-{MAX_RESULTS_LIMIT} "
            f"(default {DEFAULT_MAX_RESULTS})."
        ),
    )
    parser.add_argument(
        "--json-out",
        default=None,
        help="Optional path to write the full report as JSON.",
    )
    args = parser.parse_args(argv)
    if args.queries < 1:
        parser.error("--queries must be at least 1.")
    if args.repeats < 1:
        parser.error("--repeats must be at least 1.")
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1.")
    if not 1 <= args.max_results <= MAX_RESULTS_LIMIT:
        parser.error(f"--max-results must be between 1 and {MAX_RESULTS_LIMIT}.")
    return args


def make_queries(count: int, token: str) -> list[str]:
    """`count` unique query strings sharing `token`.

    The token is the anti-caching device: a fresh one per measurement means no
    two benchmark calls ever ask the provider the identical question, so
    provider-side caching cannot flatten the measured latency.
    """
    return [f"firecrawl latency benchmark {token} probe {index}" for index in range(count)]


def median(values: Sequence[float]) -> float:
    values = list(values)
    if not values:
        raise ValueError("median() needs at least one value")
    return float(statistics.median(values))


def summarize(times: Sequence[float]) -> dict[str, float | int]:
    times = [float(value) for value in times]
    if not times:
        raise ValueError("summarize() needs at least one sample")
    return {
        "runs": len(times),
        "median_s": median(times),
        "min_s": min(times),
        "max_s": max(times),
    }


def compute_speedup(standalone_seconds: float, parallel_seconds: float) -> float:
    """How much faster the parallel batch is than the N standalone calls.

    A value of 4.0 means the batch finished in a quarter of the standalone
    wall-clock time. `inf` marks a zero/near-zero baseline, which only happens
    when every call failed and nothing real was measured.
    """
    if parallel_seconds <= 0:
        return float("inf")
    return standalone_seconds / parallel_seconds


def build_report(
    config: dict[str, int],
    mode_times: dict[str, list[float]],
    failures: dict[str, int],
) -> dict:
    modes = {name: summarize(mode_times[name]) for name in MODE_ORDER if name in mode_times}
    speedup = compute_speedup(
        modes[MODE_STANDALONE]["median_s"],
        modes[MODE_BATCH_PARALLEL]["median_s"],
    )
    return {
        "config": config,
        "modes": modes,
        "failures": dict(failures),
        "speedup_standalone_over_parallel": speedup,
    }


def standalone_command(
    query: str, max_results: int, python: str | None = None
) -> list[str]:
    executable = python or sys.executable
    return [
        executable,
        "-m",
        "firecrawl_skill",
        "search",
        query,
        "--max-results",
        str(max_results),
    ]


def batch_command(
    queries: Sequence[str],
    concurrency: int,
    max_results: int,
    serial: bool = False,
    python: str | None = None,
) -> list[str]:
    executable = python or sys.executable
    command = [executable, "-m", "firecrawl_skill", "search"]
    for query in queries:
        command += ["--query", query]
    command += ["--max-results", str(max_results), "--concurrency", str(concurrency)]
    if serial:
        command += ["--serial"]
    return command


def usage_command(python: str | None = None) -> list[str]:
    executable = python or sys.executable
    return [executable, "-m", "firecrawl_skill", "usage", "--stdout"]


def make_child_env(base_env: dict[str, str], output_dir: Path) -> dict[str, str]:
    env = dict(base_env)
    env[OUTPUT_DIR_ENV] = str(output_dir)
    return env


# ---------------------------------------------------------------------------
# Subprocess orchestration
# ---------------------------------------------------------------------------


def make_runner(cwd: Path, env: dict[str, str]) -> Callable[[Sequence[str]], tuple[float, int]]:
    cwd_str = str(cwd)

    def run(command: Sequence[str]) -> tuple[float, int]:
        start = _clock()
        proc = subprocess.run(
            list(command),
            cwd=cwd_str,
            env=env,
            capture_output=True,
            text=True,
        )
        return _clock() - start, proc.returncode

    return run


def preflight(runner: Callable[[Sequence[str]], tuple[float, int]]) -> int:
    """One free `usage` call. Fails fast when no key resolves, before credits."""
    _, returncode = runner(usage_command())
    return returncode


def run_mode(
    mode: str,
    count: int,
    max_results: int,
    repeats: int,
    concurrency: int,
    token_factory: Callable[[], str],
    runner: Callable[[Sequence[str]], tuple[float, int]],
) -> tuple[list[float], int]:
    """Return (per-repeat wall-clock totals, failed-call count) for one mode."""
    totals: list[float] = []
    failures = 0
    for _ in range(repeats):
        queries = make_queries(count, token_factory())
        if mode == MODE_STANDALONE:
            total = 0.0
            for query in queries:
                elapsed, returncode = runner(standalone_command(query, max_results))
                total += elapsed
                failures += int(returncode != 0)
        else:
            serial = mode == MODE_BATCH_SERIAL
            elapsed, returncode = runner(
                batch_command(queries, concurrency, max_results, serial=serial)
            )
            total = elapsed
            failures += int(returncode != 0)
        totals.append(total)
    return totals, failures


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_report(report: dict) -> None:
    config = report["config"]
    print("Firecrawl search latency benchmark")
    print(
        f"  queries={config['queries']} repeats={config['repeats']} "
        f"concurrency={config['concurrency']} max_results={config['max_results']}"
    )
    print()
    header = f"{'mode':<16}{'median (s)':>12}{'min (s)':>10}{'max (s)':>10}{'runs':>6}"
    print(header)
    print("-" * len(header))
    for mode in MODE_ORDER:
        stats = report["modes"].get(mode)
        if stats is None:
            continue
        print(
            f"{mode:<16}{stats['median_s']:>12.3f}{stats['min_s']:>10.3f}"
            f"{stats['max_s']:>10.3f}{stats['runs']:>6}"
        )
    print()
    speedup = report["speedup_standalone_over_parallel"]
    print(f"speedup (standalone total / batch-parallel): {speedup:.2f}x")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if os.environ.get(OPT_IN_ENV) != "1":
        print(f"Firecrawl latency benchmark {SKIP_MESSAGE}.")
        return 0

    repo_root = Path(__file__).resolve().parents[1]
    workdir = Path(tempfile.mkdtemp(prefix="firecrawl_latency_"))
    child_env = make_child_env(os.environ, workdir)
    runner = make_runner(repo_root, child_env)
    token_factory = lambda: uuid.uuid4().hex[:8]  # noqa: E731 - tiny local closure

    mode_times: dict[str, list[float]] = {}
    failures: dict[str, int] = {}
    try:
        if preflight(runner) != 0:
            print(
                "Preflight `usage` call failed; benchmark aborted before spending "
                "credits. Check that FIRECRAWL_API_KEY resolves from the repo root.",
                file=sys.stderr,
            )
            return 1

        for mode in MODE_ORDER:
            times, failed = run_mode(
                mode,
                args.queries,
                args.max_results,
                args.repeats,
                args.concurrency,
                token_factory,
                runner,
            )
            mode_times[mode] = times
            failures[mode] = failed
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    report = build_report(
        {
            "queries": args.queries,
            "repeats": args.repeats,
            "concurrency": args.concurrency,
            "max_results": args.max_results,
        },
        mode_times,
        failures,
    )
    print_report(report)

    if args.json_out:
        target = Path(args.json_out).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote report to {target}", file=sys.stderr)

    total_failures = sum(failures.values())
    if total_failures:
        print(
            f"Warning: {total_failures} child call(s) failed; timings may be unreliable.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

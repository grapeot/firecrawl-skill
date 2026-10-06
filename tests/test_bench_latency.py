"""Offline unit tests for benchmarks/latency.py.

Only the pure helpers and the orchestration boundary are exercised; every
subprocess call is stubbed and the clock is faked, so no network traffic and no
Firecrawl credits are ever involved.
"""
import importlib.util
import json
import types
from pathlib import Path

import pytest

BENCH_PATH = Path(__file__).resolve().parent.parent / "benchmarks" / "latency.py"

_spec = importlib.util.spec_from_file_location("bench_latency", BENCH_PATH)
latency = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(latency)


# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------


def test_parse_defaults():
    args = latency.parse_args([])
    assert args.queries == latency.DEFAULT_QUERIES == 4
    assert args.repeats == latency.DEFAULT_REPEATS == 3
    assert args.concurrency == latency.DEFAULT_CONCURRENCY == 4
    assert args.max_results == latency.DEFAULT_MAX_RESULTS == 3
    assert args.json_out is None


def test_parse_overrides():
    args = latency.parse_args(
        ["--queries", "7", "--repeats", "2", "--concurrency", "8",
         "--max-results", "5", "--json-out", "/tmp/x.json"]
    )
    assert (args.queries, args.repeats, args.concurrency, args.max_results) == (7, 2, 8, 5)
    assert args.json_out == "/tmp/x.json"


@pytest.mark.parametrize(
    "argv",
    [
        ["--queries", "0"],
        ["--repeats", "0"],
        ["--concurrency", "0"],
        ["--max-results", "0"],
        ["--max-results", str(latency.MAX_RESULTS_LIMIT + 1)],
    ],
)
def test_parse_rejects_out_of_range(argv):
    with pytest.raises(SystemExit) as excinfo:
        latency.parse_args(argv)
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# Unique-query generation
# ---------------------------------------------------------------------------


def test_make_queries_are_unique_and_tagged():
    queries = latency.make_queries(4, "abc123")
    assert len(queries) == 4
    assert len(set(queries)) == 4
    assert all("abc123" in query for query in queries)


def test_make_queries_token_is_the_anti_cache_device():
    first = latency.make_queries(3, "tok-a")
    second = latency.make_queries(3, "tok-b")
    assert set(first).isdisjoint(second)


def test_make_queries_count_zero():
    assert latency.make_queries(0, "tok") == []


# ---------------------------------------------------------------------------
# Median / speedup / report shaping
# ---------------------------------------------------------------------------


def test_median_odd_and_even():
    assert latency.median([3.0, 1.0, 2.0]) == 2.0
    assert latency.median([1.0, 2.0, 3.0, 4.0]) == 2.5


def test_median_rejects_empty():
    with pytest.raises(ValueError):
        latency.median([])


def test_summarize():
    stats = latency.summarize([2.0, 4.0, 3.0])
    assert stats == {"runs": 3, "median_s": 3.0, "min_s": 2.0, "max_s": 4.0}


def test_compute_speedup():
    assert latency.compute_speedup(12.0, 3.0) == 4.0
    assert latency.compute_speedup(0.0, 0.0) == float("inf")


def test_build_report_shape_and_speedup():
    report = latency.build_report(
        {"queries": 4, "repeats": 2, "concurrency": 4, "max_results": 3},
        {
            latency.MODE_STANDALONE: [8.0, 10.0],
            latency.MODE_BATCH_PARALLEL: [2.0, 4.0],
            latency.MODE_BATCH_SERIAL: [6.0, 6.0],
        },
        {latency.MODE_STANDALONE: 0, latency.MODE_BATCH_PARALLEL: 0, latency.MODE_BATCH_SERIAL: 1},
    )
    assert report["modes"][latency.MODE_STANDALONE]["median_s"] == 9.0
    assert report["modes"][latency.MODE_BATCH_PARALLEL]["median_s"] == 3.0
    assert report["speedup_standalone_over_parallel"] == 3.0
    assert report["failures"][latency.MODE_BATCH_SERIAL] == 1
    assert json.dumps(report)  # must be JSON-serializable


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


def test_standalone_command_shape():
    command = latency.standalone_command("hello world", 3, python="py")
    assert command == ["py", "-m", "firecrawl_skill", "search", "hello world",
                       "--max-results", "3"]


def test_batch_command_shape_parallel():
    command = latency.batch_command(["q1", "q2"], 4, 3, python="py")
    assert command[:3] == ["py", "-m", "firecrawl_skill"]
    assert command.count("--query") == 2
    assert command[command.index("--query") + 1] == "q1"
    assert "--concurrency" in command and "4" in command
    assert "--serial" not in command


def test_batch_command_serial_is_same_plus_serial():
    parallel = latency.batch_command(["q1", "q2"], 4, 3)
    serial = latency.batch_command(["q1", "q2"], 4, 3, serial=True)
    assert serial == parallel + ["--serial"]


def test_make_child_env_isolates_output_dir():
    base = {"PATH": "/bin"}
    env = latency.make_child_env(base, Path("/tmp/bench"))
    assert env[latency.OUTPUT_DIR_ENV] == "/tmp/bench"
    assert latency.OUTPUT_DIR_ENV not in base  # base is not mutated


# ---------------------------------------------------------------------------
# Gate + orchestration (subprocess stubbed, clock faked)
# ---------------------------------------------------------------------------


def test_main_skips_without_opt_in(monkeypatch, capsys):
    monkeypatch.delenv(latency.OPT_IN_ENV, raising=False)

    def boom(*args, **kwargs):
        raise AssertionError("subprocess must not run when the benchmark is gated off")

    monkeypatch.setattr(latency.subprocess, "run", boom)
    assert latency.main([]) == 0
    out = capsys.readouterr().out
    assert "skipped" in out
    assert latency.OPT_IN_ENV in out


def test_main_runs_all_modes_with_stubbed_subprocess(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(latency.OPT_IN_ENV, "1")

    calls = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    class FakeClock:
        def __init__(self):
            self.value = 0.0

        def __call__(self):
            self.value += 0.1
            return self.value

    monkeypatch.setattr(latency.subprocess, "run", fake_run)
    monkeypatch.setattr(latency, "_clock", FakeClock())

    json_out = tmp_path / "report.json"
    rc = latency.main(
        ["--queries", "2", "--repeats", "2", "--concurrency", "2", "--json-out", str(json_out)]
    )
    assert rc == 0

    # 1 preflight + 2 queries * 2 repeats standalone + 2 parallel + 2 serial = 9
    assert len(calls) == 9
    assert calls[0] == latency.usage_command()

    report = json.loads(json_out.read_text())
    # Standalone pays 2 calls * 0.1s, parallel pays one call * 0.1s -> 2.0x.
    assert report["speedup_standalone_over_parallel"] == pytest.approx(2.0)
    assert report["modes"][latency.MODE_STANDALONE]["median_s"] == pytest.approx(0.2)
    assert report["modes"][latency.MODE_BATCH_PARALLEL]["median_s"] == pytest.approx(0.1)
    out = capsys.readouterr().out
    assert "speedup" in out


def test_main_aborts_when_preflight_fails(monkeypatch, capsys):
    monkeypatch.setenv(latency.OPT_IN_ENV, "1")

    calls = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(latency.subprocess, "run", fake_run)
    assert latency.main(["--queries", "2", "--repeats", "1"]) == 1
    assert len(calls) == 1  # only the preflight ran; no credits spent
    assert "aborted" in capsys.readouterr().err

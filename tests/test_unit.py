"""Offline unit tests: no network, no API key required."""
import argparse
import json
import os

import pytest

from firecrawl_skill import cli


# ---------------------------------------------------------------------------
# Parsers and defaults
# ---------------------------------------------------------------------------


def parse(argv):
    args = cli.build_parser().parse_args(argv)
    cli._validate_args(cli.build_parser(), args)
    return args


def test_search_defaults():
    args = parse(["search", "q"])
    assert args.max_results == cli.DEFAULT_MAX_RESULTS
    assert args.raw_content == "markdown"
    assert args.topic == "general"
    assert args.search_depth is None
    assert args.timeout == cli.DEFAULT_TIMEOUT
    assert args.include_images is False
    assert args.stdout is False


def test_extract_defaults():
    args = parse(["extract", "https://example.com"])
    assert args.urls == ["https://example.com"]
    assert args.extract_depth is None
    assert args.format == "markdown"
    assert args.query is None
    assert args.include_favicon is False


def test_missing_command_exits_usage():
    parser = cli.build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args([])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def usage_error(argv):
    parser = cli.build_parser()
    args = parser.parse_args(argv)
    with pytest.raises(SystemExit) as excinfo:
        cli._validate_args(parser, args)
    assert excinfo.value.code == 2


def test_max_results_out_of_range():
    usage_error(["search", "q", "--max-results", "0"])
    usage_error(["search", "q", "--max-results", "21"])


def test_time_range_exclusive_with_dates():
    usage_error(["search", "q", "--time-range", "week", "--start-date", "2026-01-01", "--end-date", "2026-01-31"])


def test_single_date_rejected():
    usage_error(["search", "q", "--start-date", "2026-01-01"])


def test_bad_date_rejected():
    usage_error(["search", "q", "--start-date", "01/01/2026", "--end-date", "2026-01-31"])


def test_include_exclude_domains_mutually_exclusive():
    usage_error(["search", "q", "--include-domain", "a.com", "--exclude-domain", "b.com"])


def test_finance_topic_rejected():
    usage_error(["search", "q", "--topic", "finance"])


def test_image_descriptions_rejected():
    usage_error(["search", "q", "--image-descriptions"])


def test_stdout_and_output_mutually_exclusive():
    usage_error(["search", "q", "--stdout", "--output", "x.json"])
    usage_error(["extract", "https://example.com", "--stdout", "--output", "x.json"])


def test_extract_url_limit():
    urls = [f"https://example.com/{i}" for i in range(21)]
    usage_error(["extract", *urls])


def test_chunks_per_source_requires_query():
    usage_error(["extract", "https://example.com", "--chunks-per-source", "3"])


def test_timeout_must_be_positive():
    usage_error(["search", "q", "--timeout", "0"])
    usage_error(["extract", "https://example.com", "--timeout", "-5"])


# ---------------------------------------------------------------------------
# tbs / date conversion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "time_range,expected",
    [
        ("day", "qdr:d"),
        ("week", "qdr:w"),
        ("month", "qdr:m"),
        ("year", "qdr:y"),
    ],
)
def test_build_tbs_time_range(time_range, expected):
    args = parse(["search", "q", "--time-range", time_range])
    assert cli._build_tbs(args) == expected


def test_build_tbs_dates():
    args = parse(["search", "q", "--start-date", "2026-01-05", "--end-date", "2026-03-09"])
    assert cli._build_tbs(args) == "cdr:1,cd_min:01/05/2026,cd_max:03/09/2026"


def test_build_tbs_none():
    args = parse(["search", "q"])
    assert cli._build_tbs(args) is None


# ---------------------------------------------------------------------------
# Request building (search)
# ---------------------------------------------------------------------------


def test_search_request_defaults():
    args = parse(["search", "hello world"])
    request = cli._build_search_request(args)
    assert request["query"] == "hello world"
    assert request["limit"] == 6
    assert request["sources"] == ["web"]
    assert request["timeout"] == 60_000
    assert request["scrapeOptions"] == {"formats": ["markdown"]}
    assert "tbs" not in request
    assert "includeDomains" not in request
    assert "excludeDomains" not in request


def test_search_request_raw_content_off():
    args = parse(["search", "q", "--raw-content", "off"])
    request = cli._build_search_request(args)
    assert "scrapeOptions" not in request


def test_search_request_topic_news():
    args = parse(["search", "q", "--topic", "news"])
    assert cli._build_search_request(args)["sources"] == ["news"]


def test_search_request_images():
    args = parse(["search", "q", "--images"])
    assert cli._build_search_request(args)["sources"] == ["web", "images"]


def test_search_request_domains_normalized():
    args = parse(["search", "q", "--include-domain", "Example.com", "--include-domain", "https://a.io/x"])
    request = cli._build_search_request(args)
    assert request["includeDomains"] == ["example.com", "a.io"]

    args = parse(["search", "q", "--exclude-domain", "Bad.COM"])
    request = cli._build_search_request(args)
    assert request["excludeDomains"] == ["bad.com"]


def test_search_request_country_code_and_name():
    args = parse(["search", "q", "--country", "US"])
    request = cli._build_search_request(args)
    assert request["country"] == "US"
    assert request["location"] == "US"

    args = parse(["search", "q", "--country", "Germany"])
    request = cli._build_search_request(args)
    assert "country" not in request
    assert request["location"] == "Germany"


def test_search_request_tbs_and_timeout():
    args = parse(["search", "q", "--time-range", "week", "--timeout", "30"])
    request = cli._build_search_request(args)
    assert request["tbs"] == "qdr:w"
    assert request["timeout"] == 30_000


# ---------------------------------------------------------------------------
# Request building (extract)
# ---------------------------------------------------------------------------


def test_extract_formats_markdown_only():
    args = parse(["extract", "https://example.com"])
    assert cli._build_extract_formats(args) == ["markdown"]


def test_extract_formats_full():
    args = parse(["extract", "https://example.com", "--images", "--query", "what is this", "--favicon"])
    formats = cli._build_extract_formats(args)
    assert formats[0] == "markdown"
    assert "images" in formats
    assert {"type": "highlights", "query": "what is this"} in formats


def test_extract_request_shape():
    args = parse(["extract", "https://example.com", "--timeout", "45"])
    request = cli._build_extract_request(args, "https://example.com")
    assert request["url"] == "https://example.com"
    assert request["timeout"] == 45_000
    assert request["formats"] == ["markdown"]


# ---------------------------------------------------------------------------
# Normalization (search)
# ---------------------------------------------------------------------------


def test_normalize_search_web(search_with_content_fixture):
    args = parse(["search", "firecrawl web scraping", "--max-results", "2"])
    out = cli._normalize_search_response(args, search_with_content_fixture)
    assert out["command"] == "search"
    data = out["data"]
    assert data["result_count"] == len(data["results"])
    assert data["credits_used"] == search_with_content_fixture["creditsUsed"]
    first = data["results"][0]
    assert "raw_content" in first
    assert "markdown" not in first
    assert isinstance(first["raw_content"], str) and len(first["raw_content"]) > 100
    for key in ("url", "title", "description", "position"):
        assert key in first
    assert out["input"]["max_results"] == 2
    assert out["input"]["search_depth"] == "advanced"


def test_normalize_search_raw_content_off(search_with_content_fixture):
    args = parse(["search", "q", "--max-results", "2", "--raw-content", "off"])
    out = cli._normalize_search_response(args, search_with_content_fixture)
    for item in out["data"]["results"]:
        assert "raw_content" not in item
        assert "markdown" not in item


def test_normalize_search_news(search_news_fixture):
    args = parse(["search", "OpenAI", "--max-results", "3", "--topic", "news"])
    out = cli._normalize_search_response(args, search_news_fixture)
    news = search_news_fixture["data"]["news"]
    results = out["data"]["results"]
    assert len(results) == len(news)
    for expected, got in zip(news, results):
        assert got["url"] == expected["url"]
        assert got["title"] == expected["title"]
        assert got["date"] == expected["date"]
    assert out["input"]["topic"] == "news"


def test_normalize_search_news_snippet_fallback(search_news_fixture):
    fixture = json.loads(json.dumps(search_news_fixture))
    fixture["data"]["news"][0].pop("description", None)
    args = parse(["search", "q", "--topic", "news"])
    out = cli._normalize_search_response(args, fixture)
    assert out["data"]["results"][0].get("description") == fixture["data"]["news"][0].get("snippet")


def test_normalize_search_image_count():
    fixture = {"success": True, "creditsUsed": 2, "id": "job-1", "data": {"web": [{"url": "https://a.com", "title": "A"}], "images": [{"url": "https://img/1.jpg"}, {"url": "https://img/2.jpg"}]}}
    args = parse(["search", "q"])
    out = cli._normalize_search_response(args, fixture)
    assert out["data"]["image_count"] == 2
    assert out["data"]["job_id"] == "job-1"


# ---------------------------------------------------------------------------
# Normalization (extract)
# ---------------------------------------------------------------------------


def test_normalize_extract_result(scrape_ok_fixture):
    args = parse(["extract", "https://example.com"])
    item = cli._normalize_extract_result(args, "https://example.com", scrape_ok_fixture)
    assert item["url"] == "https://example.com"
    assert item["error"] is None
    assert len(item["markdown"]) > 50
    assert item["metadata"]["url"].rstrip("/") == "https://example.com"
    # favicon stripped unless --favicon
    assert "favicon" not in item["metadata"]
    assert "highlights" not in item


def test_normalize_extract_result_favicon_kept(scrape_ok_fixture):
    args = parse(["extract", "https://example.com", "--favicon"])
    item = cli._normalize_extract_result(args, "https://example.com", scrape_ok_fixture)
    assert "favicon" in item["metadata"]


def test_normalize_extract_highlights(scrape_highlights_fixture):
    args = parse(["extract", "https://example.com", "--query", "domain registration"])
    item = cli._normalize_extract_result(args, "https://example.com", scrape_highlights_fixture)
    assert isinstance(item["highlights"], list)
    assert "markdown" in scrape_highlights_fixture["data"]


def test_normalize_highlights_variants():
    assert cli._normalize_highlights(None) == []
    assert cli._normalize_highlights("") == []
    assert cli._normalize_highlights("one") == ["one"]
    assert cli._normalize_highlights(["a", "", "b"]) == ["a", "b"]
    assert cli._normalize_highlights(7) == ["7"]


def test_scrape_credits_used_variants():
    assert cli._scrape_credits_used({"creditsUsed": 7}) == 7
    assert cli._scrape_credits_used({"data": {"metadata": {"creditsUsed": 5}}}) == 5
    assert cli._scrape_credits_used({"data": {"metadata": {"creditCount": 2}}}) == 2
    assert cli._scrape_credits_used({}) is None


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,payload,expected",
    [
        (200, {"success": True}, 0),
        (200, {"success": False}, 12),
        (200, None, 12),
        (401, None, 10),
        (403, {"error": "forbidden"}, 10),
        (402, None, 11),
        (429, {"error": "rate limited"}, 11),
        (400, {"error": "bad query"}, 12),
        (404, None, 12),
        (408, None, 13),
        (500, None, 13),
        (502, None, 13),
        (503, None, 13),
        (504, None, 13),
        (None, None, 13),
    ],
)
def test_exit_code_for(status, payload, expected):
    assert cli._exit_code_for(status, payload) == expected


def test_error_message_prefers_upstream_error():
    assert "bad query" in cli._error_message(400, {"error": "bad query"})
    assert cli._error_message(None, None) == "network error (timeout or connection failure)"
    assert cli._error_message(500, None) == "HTTP 500"
    assert cli._error_message(200, {"success": False}) == "upstream returned success=false or an unparseable response"


# ---------------------------------------------------------------------------
# Credit estimation
# ---------------------------------------------------------------------------


def test_estimate_search_credits():
    args = parse(["search", "q"])
    assert cli._estimate_search_credits(args) == 8  # 2 search + 6 scrapes
    args = parse(["search", "q", "--max-results", "20", "--raw-content", "off"])
    assert cli._estimate_search_credits(args) == 4  # 2 * ceil(20/10), no scrapes
    args = parse(["search", "q", "--max-results", "13"])
    assert cli._estimate_search_credits(args) == 17  # 2 * ceil(13/10) + 13


def test_estimate_extract_credits():
    args = parse(["extract", "https://a.com", "https://b.com", "https://c.com"])
    assert cli._estimate_extract_credits(args) == 3
    args = parse(["extract", "https://a.com", "https://b.com", "--query", "x"])
    assert cli._estimate_extract_credits(args) == 10  # (1 + 4) * 2


# ---------------------------------------------------------------------------
# Slugs and output paths
# ---------------------------------------------------------------------------


def test_slugify():
    assert cli._slugify("Firecrawl, The Next-Gen!") == "firecrawl_the_next_gen"
    assert cli._slugify("///") == "payload"
    assert len(cli._slugify("x" * 100)) <= 48


def test_default_output_path_search(tmp_path, monkeypatch):
    monkeypatch.setenv(cli._OUTPUT_DIR_ENV, str(tmp_path))
    args = parse(["search", "hello world"])
    path = cli._default_output_path(args)
    assert path.startswith(str(tmp_path))
    assert "hello_world" in path


# ---------------------------------------------------------------------------
# .env loading and key resolution
# ---------------------------------------------------------------------------


def test_load_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("FOO_TEST_KEY=bar\n# comment\nEMPTY=\nQUOTED='q v'\n", encoding="utf-8")
    for var in ("FOO_TEST_KEY", "EMPTY", "QUOTED"):
        monkeypatch.delenv(var, raising=False)
    loaded = cli.load_workspace_env(str(env_file))
    assert loaded == env_file
    assert os.environ["FOO_TEST_KEY"] == "bar"
    assert os.environ["QUOTED"] == "q v"


def test_load_env_file_does_not_override(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("PRESET_VAR=from_file\n", encoding="utf-8")
    monkeypatch.setenv("PRESET_VAR", "from_env")
    cli.load_workspace_env(str(env_file))
    assert os.environ["PRESET_VAR"] == "from_env"


def test_load_env_file_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # avoid picking up .env from ancestor directories
    assert cli.load_workspace_env(str(tmp_path / "nope.env")) is None


def test_get_api_key_from_env(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.delenv(cli._ONEPASSWORD_REF_ENV, raising=False)
    assert cli._get_api_key() == "fc-test-key"


def test_get_api_key_missing_raises(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.delenv(cli._ONEPASSWORD_REF_ENV, raising=False)
    with pytest.raises(cli.ApiError, match="FIRECRAWL_API_KEY"):
        cli._get_api_key()


def test_get_api_key_bad_op_reference_raises(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.setenv(cli._ONEPASSWORD_REF_ENV, "op://nonexistent/vault/item/field")
    with pytest.raises(cli.ApiError):
        cli._get_api_key()


# ---------------------------------------------------------------------------
# Output emission
# ---------------------------------------------------------------------------


def test_emit_payload_stdout(capsys):
    payload = {"command": "search", "input": {}, "data": {"result_count": 1}}
    cli._emit_payload(payload, None)
    assert json.loads(capsys.readouterr().out) == payload


def test_emit_payload_file(capsys, tmp_path):
    out_file = tmp_path / "out.json"
    payload = {"command": "search", "input": {}, "data": {"result_count": 2, "image_count": 0}}
    cli._emit_payload(payload, str(out_file))
    assert json.loads(out_file.read_text()) == payload
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "ok"
    assert status["output_mode"] == "file"
    assert status["output_path"] == str(out_file)
    assert status["summary"]["result_count"] == 2
    assert "payload_schema" in status


# ---------------------------------------------------------------------------
# main() with stubbed transport
# ---------------------------------------------------------------------------


def test_main_search_success(capsys, monkeypatch, search_with_content_fixture):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(cli, "_post_json", lambda path, body, key, timeout: (200, search_with_content_fixture))
    args = cli.build_parser().parse_args(["search", "firecrawl", "--max-results", "2", "--stdout"])
    cli._validate_args(cli.build_parser(), args)
    rc = cli.main(["search", "firecrawl", "--max-results", "2", "--stdout"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["command"] == "search"
    assert out["data"]["result_count"] == 2


def test_main_search_auth_error(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # avoid .env resolving a real key
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.delenv(cli._ONEPASSWORD_REF_ENV, raising=False)
    rc = cli.main(["search", "q", "--stdout"])
    assert rc == cli.EXIT_AUTH
    lines = [l for l in capsys.readouterr().err.splitlines() if l.strip()]
    err = json.loads(lines[-1])
    assert err["command"] == "search"
    assert "FIRECRAWL_API_KEY" in err["error"]


def test_main_search_upstream_error(capsys, monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(cli, "_post_json", lambda path, body, key, timeout: (429, {"error": "credits exhausted"}))
    rc = cli.main(["search", "q", "--stdout"])
    assert rc == cli.EXIT_QUOTA_RATE
    lines = [l for l in capsys.readouterr().err.splitlines() if l.strip()]
    err = json.loads(lines[-1])
    assert err["http_status"] == 429
    assert "credits exhausted" in err["error"]


def test_main_extract_partial_failure(capsys, monkeypatch, scrape_ok_fixture):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")

    def fake_post(path, body, key, timeout):
        if body["url"] == "https://bad.example":
            return 500, None
        return 200, scrape_ok_fixture

    monkeypatch.setattr(cli, "_post_json", fake_post)
    rc = cli.main(["extract", "https://example.com", "https://bad.example", "--stdout"])
    assert rc == 0
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["data"]["result_count"] == 1
    assert out["data"]["failed_count"] == 1
    assert out["data"]["failed_results"][0]["url"] == "https://bad.example"
    assert "Warning" in captured.err


def test_main_extract_all_failed(capsys, monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(cli, "_post_json", lambda path, body, key, timeout: (401, None))
    rc = cli.main(["extract", "https://example.com", "--stdout"])
    assert rc == cli.EXIT_AUTH


def test_main_search_file_output(capsys, tmp_path, monkeypatch, search_with_content_fixture):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(cli, "_post_json", lambda path, body, key, timeout: (200, search_with_content_fixture))
    out_file = tmp_path / "result.json"
    rc = cli.main(["search", "firecrawl", "--max-results", "2", "--output", str(out_file)])
    assert rc == 0
    assert out_file.exists()
    status = json.loads(capsys.readouterr().out)
    assert status["output_path"] == str(out_file)

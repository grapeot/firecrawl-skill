"""Offline unit tests: no network, no API key required."""
import argparse
import contextlib
import json
import os
import time
from pathlib import Path

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


def test_usage_defaults():
    args = parse(["usage"])
    assert args.timeout == cli.DEFAULT_TIMEOUT
    assert args.history == 0
    assert args.stdout is False
    assert args.output is None


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


def test_usage_history_out_of_range():
    usage_error(["usage", "--history", "-1"])
    usage_error(["usage", "--history", str(cli.MAX_USAGE_PERIODS + 1)])


def test_usage_timeout_must_be_positive():
    usage_error(["usage", "--timeout", "0"])


def test_usage_stdout_and_output_mutually_exclusive():
    usage_error(["usage", "--stdout", "--output", "x.json"])


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
# Cost breakdown (observability)
# ---------------------------------------------------------------------------


def test_base_search_credits():
    assert cli._base_search_credits(0) == 0
    assert cli._base_search_credits(1) == 2
    assert cli._base_search_credits(10) == 2
    assert cli._base_search_credits(11) == 4
    assert cli._base_search_credits(20) == 4


def test_as_int_rejects_bool_and_strings():
    assert cli._as_int(30) == 30
    assert cli._as_int(1.9) == 1
    assert cli._as_int(True) is None
    assert cli._as_int(False) is None
    assert cli._as_int("30") is None
    assert cli._as_int(None) is None


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://x.com/foo", "x"),
        ("https://x.com./foo", "x"),          # FQDN trailing dot
        ("https://X.COM/foo", "x"),            # case
        ("https://mobile.twitter.com/a", "x"),
        ("https://x.com:8443/a", "x"),         # port
        ("https://user@x.com/a", "x"),         # userinfo
        ("https://evilx.com/a", "html"),       # must NOT match x.com
        ("https://x.com.evil.com/a", "html"),  # must NOT match x.com
        ("https://notx.com/a", "html"),
        ("https://x.com.cn/a", "html"),
        ("not a url", "html"),
        (None, "html"),
    ],
)
def test_document_kind_host_matching(url, expected):
    assert cli._document_kind(url, {"contentType": "text/html"}) == expected


def test_document_cost_bool_credits_ignored():
    # upstream JSON true must not be read as a 1-credit figure
    kind, credits, _ = cli._document_cost("https://x.com/a", {"creditsUsed": True}, None)
    assert kind == "x" and credits == 30


def test_search_cost_breakdown_plain_html():
    # 2 HTML docs, upstream reported 2 (base) + 1 + 1 = 4
    results = [
        {"url": "https://a.com", "raw_content": "x", "metadata": {"contentType": "text/html", "creditsUsed": 1}},
        {"url": "https://b.com", "raw_content": "y", "metadata": {"contentType": "text/html", "creditsUsed": 1}},
    ]
    cb = cli._build_cost_breakdown(
        cli._base_search_credits(len(results)), cli._cost_documents(results, "raw_content"), 4
    )
    assert cb["base"] == 2
    assert [d["kind"] for d in cb["documents"]] == ["html", "html"]
    assert cb["modelled_total"] == 4
    assert cb["reconciles"] is True
    assert cb["warning"] is None


def test_search_cost_breakdown_pdf_billed_per_page():
    results = [
        {"url": "https://x/visa.pdf", "raw_content": "p", "metadata": {"contentType": "application/pdf", "numPages": 67, "creditsUsed": 67}},
        {"url": "https://x/a.html", "raw_content": "h", "metadata": {"contentType": "text/html; charset=utf-8", "creditsUsed": 1}},
    ]
    cb = cli._build_cost_breakdown(cli._base_search_credits(len(results)), cli._cost_documents(results, "raw_content"), 70)
    assert cb["documents"][0]["kind"] == "pdf"
    assert cb["documents"][0]["pages"] == 67
    assert cb["modelled_total"] == 2 + 67 + 1
    assert cb["reconciles"] is True


def test_search_cost_breakdown_x_grok():
    results = [
        {"url": "https://x.com/foo/status/1", "raw_content": "s", "metadata": {"creditsUsed": 30}},
        {"url": "https://twitter.com/bar", "raw_content": "s", "metadata": {"creditsUsed": 30}},
        {"url": "https://x.com/foo/all", "metadata": {}},  # list page, no document -> 0
    ]
    cb = cli._build_cost_breakdown(cli._base_search_credits(len(results)), cli._cost_documents(results, "raw_content"), 62)
    assert [d["kind"] for d in cb["documents"]] == ["x", "x", "x"]
    assert [d["credits"] for d in cb["documents"]] == [30, 30, 0]
    assert cb["modelled_total"] == 62
    assert cb["reconciles"] is True


def test_search_cost_breakdown_no_document_is_zero():
    results = [{"url": "https://x.com/foo/all", "metadata": {"viewport": "x"}}]  # metadata but no doc
    cb = cli._build_cost_breakdown(cli._base_search_credits(len(results)), cli._cost_documents(results, "raw_content"), 2)
    assert cb["documents"][0]["credits"] == 0
    assert cb["modelled_total"] == 2
    assert cb["reconciles"] is True


def test_search_cost_breakdown_mismatch_warns_not_blocks():
    results = [{"url": "https://a.com", "raw_content": "x", "metadata": {"contentType": "text/html", "creditsUsed": 1}}]
    cb = cli._build_cost_breakdown(cli._base_search_credits(len(results)), cli._cost_documents(results, "raw_content"), 999)
    assert cb["reconciles"] is False
    assert cb["reported_total"] == 999
    assert isinstance(cb["warning"], str) and "999" in cb["warning"]


def test_search_cost_breakdown_no_reported_total():
    results = [{"url": "https://a.com", "raw_content": "x", "metadata": {"contentType": "text/html"}}]
    cb = cli._build_cost_breakdown(cli._base_search_credits(len(results)), cli._cost_documents(results, "raw_content"), None)
    assert cb["reported_total"] is None
    assert cb["reconciles"] is None
    assert cb["warning"] is None


def test_normalize_search_includes_cost_breakdown(search_with_content_fixture):
    args = parse(["search", "firecrawl web scraping", "--max-results", "2"])
    out = cli._normalize_search_response(args, search_with_content_fixture)
    cb = out["data"]["cost_breakdown"]
    assert cb["reported_total"] == search_with_content_fixture["creditsUsed"]
    assert cb["reconciles"] is True


def test_extract_cost_documents():
    results = [
        {"url": "https://a.com", "markdown": "x", "metadata": {"contentType": "text/html", "creditsUsed": 5}},  # highlights +4
        {"url": "https://x/b.pdf", "markdown": "y", "metadata": {"contentType": "application/pdf", "numPages": 32, "creditsUsed": 32}},
    ]
    docs = cli._cost_documents(results, "markdown")
    assert docs[0]["credits"] == 5
    assert docs[1]["kind"] == "pdf" and docs[1]["pages"] == 32
    cb = cli._build_cost_breakdown(0, docs, 37)
    assert cb["reconciles"] is True


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
# Normalization (usage)
# ---------------------------------------------------------------------------


def test_normalize_usage_response(credit_usage_fixture):
    args = parse(["usage"])
    out = cli._normalize_usage_response(args, credit_usage_fixture, None)
    assert out["command"] == "usage"
    data = out["data"]
    upstream = credit_usage_fixture["data"]
    assert data["provider"] == "firecrawl"
    assert data["remaining_credits"] == upstream["remainingCredits"]
    assert data["plan_credits"] == upstream["planCredits"]
    assert data["credits_used_in_period"] == (
        upstream["planCredits"] - upstream["remainingCredits"]
    )
    assert data["billing_period_start"] == upstream["billingPeriodStart"]
    assert data["billing_period_end"] == upstream["billingPeriodEnd"]
    assert data["periods"] == []
    # `raw` is the upstream body verbatim — nothing invented, nothing dropped.
    assert data["raw"] == {"credit_usage": credit_usage_fixture}
    assert "credit_usage_historical" not in data["raw"]


def test_normalize_usage_response_with_history(credit_usage_fixture, credit_usage_historical_fixture):
    args = parse(["usage", "--history", "4"])
    out = cli._normalize_usage_response(args, credit_usage_fixture, credit_usage_historical_fixture)
    data = out["data"]
    assert len(data["periods"]) == len(credit_usage_historical_fixture["periods"])
    first = credit_usage_historical_fixture["periods"][0]
    assert data["periods"][0] == {
        "start_date": first["startDate"],
        "end_date": first["endDate"],
        "credits_used": first["creditsUsed"],
    }
    assert data["raw"]["credit_usage_historical"] == credit_usage_historical_fixture


def test_normalize_usage_period_open_ended(credit_usage_historical_fixture):
    period = credit_usage_historical_fixture["periods"][-1]
    normalized = cli._normalize_period(period)
    assert normalized["end_date"] == period["endDate"]
    assert normalized["credits_used"] == period["creditsUsed"]
    assert "api_key" not in normalized  # periods never project apiKey (see D6)


def test_normalize_period_total_credits_alias():
    # The published OpenAPI example names this field totalCredits; the live API
    # names it creditsUsed. Both must normalize to the same place.
    assert cli._normalize_period({"startDate": "a", "endDate": "b", "totalCredits": 12})["credits_used"] == 12
    assert cli._normalize_period({"startDate": "a", "endDate": "b", "creditsUsed": 12, "totalCredits": 99})[
        "credits_used"
    ] == 12


def test_normalize_period_never_projects_api_key():
    # The CLI never sends byApiKey=true, so a key name must not reach `periods`
    # (it stays in `data.raw`). Same rule for a null or a real name.
    assert cli._normalize_period({"startDate": "a", "endDate": "b", "creditsUsed": 1, "apiKey": None}) == {
        "start_date": "a",
        "end_date": "b",
        "credits_used": 1,
    }
    assert "api_key" not in cli._normalize_period(
        {"startDate": "a", "endDate": "b", "creditsUsed": 1, "apiKey": "Default"}
    )


def test_normalize_usage_input_records_by_api_key(credit_usage_fixture, credit_usage_historical_fixture):
    # byApiKey is only meaningful once history is actually fetched.
    assert "by_api_key" not in cli._normalize_usage_response(parse(["usage"]), credit_usage_fixture, None)["input"]
    out = cli._normalize_usage_response(
        parse(["usage", "--history", "2"]), credit_usage_fixture, credit_usage_historical_fixture
    )
    assert out["input"]["by_api_key"] is False


def test_normalize_usage_input_records_history_count(credit_usage_fixture):
    out = cli._normalize_usage_response(parse(["usage", "--history", "7"]), credit_usage_fixture, None)
    assert out["input"]["history"] == 7


def test_normalize_usage_response_missing_fields():
    # Unknown/partial upstream bodies must not fabricate numbers.
    out = cli._normalize_usage_response(parse(["usage"]), {"success": True, "data": {}}, None)
    data = out["data"]
    assert data["remaining_credits"] is None
    assert data["plan_credits"] is None
    assert data["credits_used_in_period"] is None
    assert data["billing_period_start"] is None
    assert data["raw"] == {"credit_usage": {"success": True, "data": {}}}


def test_normalize_usage_response_missing_data_key():
    out = cli._normalize_usage_response(parse(["usage"]), {"success": True}, None)
    assert out["data"]["remaining_credits"] is None


def test_normalize_usage_response_non_dict_data():
    # A truthy non-dict `data` (list/str/number) must not crash normalization;
    # it degrades to null fields rather than raising AttributeError.
    for bad in ([1, 2, 3], "unexpected", 42):
        out = cli._normalize_usage_response(parse(["usage"]), {"success": True, "data": bad}, None)
        assert out["data"]["remaining_credits"] is None
        assert out["data"]["plan_credits"] is None


def test_normalize_usage_response_non_dict_periods():
    # Historical periods that are not dicts must be skipped, not crash.
    out = cli._normalize_usage_response(
        parse(["usage", "--history", "3"]),
        {"success": True, "data": {"remainingCredits": 1, "planCredits": 2}},
        {"periods": [None, "x", {"startDate": "a", "endDate": "b", "creditsUsed": 5}]},
    )
    assert out["data"]["periods"] == [{"start_date": "a", "end_date": "b", "credits_used": 5}]


def test_int_or_none():
    assert cli._int_or_none(7) == 7
    assert cli._int_or_none(7.0) == 7
    assert cli._int_or_none(None) is None
    assert cli._int_or_none("7") is None
    assert cli._int_or_none(True) is None


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


def test_default_output_path_usage(tmp_path, monkeypatch):
    monkeypatch.setenv(cli._OUTPUT_DIR_ENV, str(tmp_path))
    args = parse(["usage"])
    path = cli._default_output_path(args)
    assert path.startswith(str(tmp_path))
    assert "usage_" in path and path.endswith("_credits.json")


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
    # The summary block is shared across commands; non-usage commands carry a null
    # remaining_credits so consumers can read one shape everywhere.
    assert status["summary"]["remaining_credits"] is None
    assert set(status["summary"]) == {
        "result_count",
        "failed_count",
        "image_count",
        "credits_used",
        "remaining_credits",
    }


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


def test_main_search_text_maps_to_markdown_note(capsys, monkeypatch, search_with_content_fixture):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(cli, "_post_json", lambda path, body, key, timeout: (200, search_with_content_fixture))
    rc = cli.main(["search", "q", "--max-results", "2", "--raw-content", "text", "--stdout"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "text maps to markdown" in captured.err
    out = json.loads(captured.out)
    assert out["data"]["results"][0]["raw_content"] is not None


def test_main_extract_format_text_note(capsys, monkeypatch, scrape_ok_fixture):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(cli, "_post_json", lambda path, body, key, timeout: (200, scrape_ok_fixture))
    rc = cli.main(["extract", "https://example.com", "--format", "text", "--stdout"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "text maps to markdown" in captured.err
    assert json.loads(captured.out)["data"]["result_count"] == 1


def test_main_search_file_output(capsys, tmp_path, monkeypatch, search_with_content_fixture):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(cli, "_post_json", lambda path, body, key, timeout: (200, search_with_content_fixture))
    out_file = tmp_path / "result.json"
    rc = cli.main(["search", "firecrawl", "--max-results", "2", "--output", str(out_file)])
    assert rc == 0
    assert out_file.exists()
    status = json.loads(capsys.readouterr().out)
    assert status["output_path"] == str(out_file)


# ---------------------------------------------------------------------------
# usage: run_usage and main() with stubbed transport
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def monkeypatched_get(fake_get):
    original = cli._get_json
    cli._get_json = fake_get
    try:
        yield
    finally:
        cli._get_json = original


def stub_get(responses):
    """Return a _get_json replacement serving (status, body) per call in order."""
    calls = []

    def fake_get(path, key, timeout, params=None):
        calls.append({"path": path, "timeout": timeout, "params": params})
        index = len(calls) - 1
        if index < len(responses):
            return responses[index]
        return 404, {"success": False, "error": "unexpected call"}

    return fake_get, calls


def test_run_usage_success(credit_usage_fixture):
    args = parse(["usage"])
    fake_get, calls = stub_get([(200, credit_usage_fixture)])
    with monkeypatched_get(fake_get):
        code, payload, error = cli.run_usage(args, "fc-test-key")
    assert code == 0
    assert error is None
    assert payload["data"]["remaining_credits"] == credit_usage_fixture["data"]["remainingCredits"]
    assert calls[0]["path"] == cli.USAGE_PATH
    assert calls[0]["timeout"] == cli.DEFAULT_TIMEOUT
    assert calls[0]["params"] is None


def test_run_usage_history_dedupes_current_period(live_usage_pair):
    current, historical = live_usage_pair
    args = parse(["usage", "--history", "4"])
    fake_get, calls = stub_get([(200, current), (200, historical)])
    with monkeypatched_get(fake_get):
        code, payload, _ = cli.run_usage(args, "fc-test-key")
    assert code == 0
    assert calls[1]["path"] == cli.USAGE_HISTORICAL_PATH
    # The historical endpoint repeats the open current period, which the primary
    # call already reports; it must appear once, in the balance fields only.
    starts = [p_["start_date"] for p_ in payload["data"]["periods"]]
    assert current["data"]["billingPeriodStart"] not in starts
    assert starts == ["2024-07-01T00:00:00.000Z", "2024-08-01T00:00:00.000Z", "2024-09-01T00:00:00.000Z"]


def test_run_usage_history_caps_at_n(live_usage_pair):
    current, historical = live_usage_pair
    args = parse(["usage", "--history", "2"])
    fake_get, _ = stub_get([(200, current), (200, historical)])
    with monkeypatched_get(fake_get):
        _, payload, _ = cli.run_usage(args, "fc-test-key")
    starts = [p_["start_date"] for p_ in payload["data"]["periods"]]
    assert starts == ["2024-07-01T00:00:00.000Z", "2024-08-01T00:00:00.000Z"]


def test_run_usage_history_keeps_extra_open_periods(live_usage_pair):
    """Only the current period is deduped; other open-ended periods are kept."""
    current, historical = live_usage_pair
    current = json.loads(json.dumps(current))
    current["data"]["billingPeriodStart"] = "2024-10-01T00:00:00.000Z"
    historical["periods"][0]["endDate"] = None  # a second, genuinely open period
    args = parse(["usage", "--history", "10"])
    fake_get, _ = stub_get([(200, current), (200, historical)])
    with monkeypatched_get(fake_get):
        _, payload, _ = cli.run_usage(args, "fc-test-key")
    starts = [p_["start_date"] for p_ in payload["data"]["periods"]]
    assert "2024-07-01T00:00:00.000Z" in starts  # open period survives
    assert "2024-10-01T00:00:00.000Z" not in starts  # current period deduped


def test_run_usage_history_api_key_not_requested(live_usage_pair):
    """byApiKey stays at its default: no API-key names enter the normalized periods."""
    current, historical = live_usage_pair
    assert "apiKey" in historical["periods"][0]  # upstream does send it in some shapes
    args = parse(["usage", "--history", "4"])
    fake_get, calls = stub_get([(200, current), (200, historical)])
    with monkeypatched_get(fake_get):
        _, payload, _ = cli.run_usage(args, "fc-test-key")
    assert calls[1]["params"] is None  # no byApiKey query param is ever sent
    assert all("api_key" not in p_ for p_ in payload["data"]["periods"])
    # but nothing is hidden from raw, so the passthrough decision stays auditable
    assert payload["data"]["raw"]["credit_usage_historical"]["periods"][0]["apiKey"] == "Default"


def test_run_usage_history_failure_is_not_fatal(capsys, credit_usage_fixture):
    args = parse(["usage", "--history", "3"])
    fake_get, _ = stub_get([(200, credit_usage_fixture), (500, {"success": False, "error": "boom"})])
    with monkeypatched_get(fake_get):
        code, payload, error = cli.run_usage(args, "fc-test-key")
    assert code == 0, error
    assert payload["data"]["periods"] == []
    assert payload["data"]["raw"]["credit_usage_historical"] == {"periods": []}
    assert "historical periods unavailable" in capsys.readouterr().err


def test_run_usage_auth_error():
    args = parse(["usage"])
    fake_get, _ = stub_get([(401, {"success": False, "error": "invalid api key"})])
    with monkeypatched_get(fake_get):
        code, payload, error = cli.run_usage(args, "fc-test-key")
    assert code == cli.EXIT_AUTH
    assert payload is None
    assert error["http_status"] == 401


def test_run_usage_not_found_maps_to_rejected(credit_usage_not_found_fixture):
    # 404 is the documented "no credit usage information" response → 12, no new codes.
    args = parse(["usage"])
    fake_get, _ = stub_get([(404, credit_usage_not_found_fixture)])
    with monkeypatched_get(fake_get):
        code, _, error = cli.run_usage(args, "fc-test-key")
    assert code == cli.EXIT_REJECTED
    assert "Could not find credit usage" in error["error"]


def test_run_usage_network_error():
    args = parse(["usage"])
    fake_get, _ = stub_get([(None, None)])
    with monkeypatched_get(fake_get):
        code, _, error = cli.run_usage(args, "fc-test-key")
    assert code == cli.EXIT_NETWORK_SERVER
    assert error["http_status"] is None


def test_run_usage_success_false_maps_to_rejected():
    args = parse(["usage"])
    fake_get, _ = stub_get([(200, {"success": False, "error": "Internal server error while fetching credit usage"})])
    with monkeypatched_get(fake_get):
        code, _, error = cli.run_usage(args, "fc-test-key")
    assert code == cli.EXIT_REJECTED


def test_run_usage_prints_no_credit_estimate(capsys, credit_usage_fixture):
    args = parse(["usage"])
    fake_get, _ = stub_get([(200, credit_usage_fixture)])
    with monkeypatched_get(fake_get):
        cli.run_usage(args, "fc-test-key")
    assert "Estimated Firecrawl credits" not in capsys.readouterr().err


def test_main_usage_stdout(capsys, monkeypatch, credit_usage_fixture):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    fake_get, _ = stub_get([(200, credit_usage_fixture)])
    monkeypatch.setattr(cli, "_get_json", fake_get)
    rc = cli.main(["usage", "--stdout"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["command"] == "usage"
    assert out["data"]["provider"] == "firecrawl"
    assert out["data"]["remaining_credits"] == credit_usage_fixture["data"]["remainingCredits"]
    assert out["data"]["raw"]["credit_usage"] == credit_usage_fixture


def test_main_usage_file_output(capsys, monkeypatch, tmp_path, credit_usage_fixture):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    fake_get, _ = stub_get([(200, credit_usage_fixture)])
    monkeypatch.setattr(cli, "_get_json", fake_get)
    out_file = tmp_path / "usage.json"
    rc = cli.main(["usage", "--output", str(out_file)])
    assert rc == 0
    assert json.loads(out_file.read_text())["data"]["remaining_credits"] == 4200
    status = json.loads(capsys.readouterr().out)
    assert status["command"] == "usage"
    assert status["output_mode"] == "file"
    assert status["output_path"] == str(out_file)
    assert status["summary"]["remaining_credits"] == 4200
    assert status["payload_schema"]["data"]["remaining_credits"] == "number|null"


def test_main_usage_default_output_path(capsys, monkeypatch, tmp_path, credit_usage_fixture):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setenv(cli._OUTPUT_DIR_ENV, str(tmp_path))
    fake_get, _ = stub_get([(200, credit_usage_fixture)])
    monkeypatch.setattr(cli, "_get_json", fake_get)
    rc = cli.main(["usage"])
    assert rc == 0
    status = json.loads(capsys.readouterr().out)
    assert status["output_path"].startswith(str(tmp_path))
    assert "usage_" in status["output_path"]
    assert Path(status["output_path"]).exists()


def test_main_usage_auth_error_without_key(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.delenv(cli._ONEPASSWORD_REF_ENV, raising=False)
    rc = cli.main(["usage", "--stdout"])
    assert rc == cli.EXIT_AUTH
    err = json.loads([l for l in capsys.readouterr().err.splitlines() if l.strip()][-1])
    assert err["command"] == "usage"


def test_main_usage_upstream_error(capsys, monkeypatch, credit_usage_not_found_fixture):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    fake_get, _ = stub_get([(404, credit_usage_not_found_fixture)])
    monkeypatch.setattr(cli, "_get_json", fake_get)
    rc = cli.main(["usage", "--stdout"])
    assert rc == cli.EXIT_REJECTED
    err = json.loads([l for l in capsys.readouterr().err.splitlines() if l.strip()][-1])
    assert err["command"] == "usage"
    assert err["http_status"] == 404


def test_main_usage_never_touches_post_transport(monkeypatch, tmp_path, credit_usage_fixture):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setattr(cli, "_post_json", unexpected_post)
    fake_get, _ = stub_get([(200, credit_usage_fixture)])
    monkeypatch.setattr(cli, "_get_json", fake_get)
    assert cli.main(["usage", "--stdout"]) == 0


def unexpected_post(path, body, key, timeout):
    raise AssertionError("usage must use GET transport, not POST")


# ---------------------------------------------------------------------------
# search: multi-query batch mode
# ---------------------------------------------------------------------------


def test_search_positional_query_is_single_mode():
    args = parse(["search", "hello world"])
    assert args.query == "hello world"
    assert args.queries == []
    assert cli._is_batch(args) is False


def test_search_query_option_is_batch_mode():
    args = parse(["search", "--query", "q1"])
    assert args.query is None
    assert args.queries == ["q1"]
    assert cli._is_batch(args) is True


def test_search_query_option_keeps_multiword_query_intact():
    args = parse(["search", "--query", "one two three"])
    assert args.queries == ["one two three"]
    assert cli._build_search_request(cli._with_query(args, args.queries[0]))["query"] == "one two three"


def test_search_query_batch_defaults():
    args = parse(["search", "--query", "q1", "--query", "q2"])
    assert args.queries == ["q1", "q2"]
    assert args.concurrency == cli.DEFAULT_CONCURRENCY
    assert args.serial is False


def test_search_positional_and_query_mutually_exclusive():
    usage_error(["search", "positional", "--query", "batch"])


def test_search_requires_a_query():
    usage_error(["search"])
    usage_error(["search", "--max-results", "3"])


def test_search_batch_rejects_output():
    usage_error(["search", "--query", "q", "--output", "x.json"])


def test_search_batch_rejects_stdout():
    usage_error(["search", "--query", "q", "--stdout"])


def test_search_concurrency_must_be_positive():
    usage_error(["search", "--query", "q", "--concurrency", "0"])
    usage_error(["search", "--query", "q", "--concurrency", "-1"])


def test_search_batch_writes_one_file_per_query(capsys, monkeypatch, tmp_path, search_with_content_fixture):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setenv(cli._OUTPUT_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(cli, "_post_json", lambda path, body, key, timeout: (200, search_with_content_fixture))

    rc = cli.main(["search", "--query", "alpha", "--query", "beta", "--query", "gamma"])
    assert rc == 0
    status = json.loads(capsys.readouterr().out)
    assert status["command"] == "search"
    assert status["output_mode"] == "batch"
    assert status["summary"] == {
        "query_count": 3,
        "success_count": 3,
        "failed_count": 0,
        "credits_used": 3 * search_with_content_fixture["creditsUsed"],
    }
    assert [entry["query"] for entry in status["results"]] == ["alpha", "beta", "gamma"]
    paths = [entry["output_path"] for entry in status["results"]]
    assert len(set(paths)) == 3
    for entry in status["results"]:
        assert entry["error"] is None
        assert entry["summary"]["result_count"] == 2
        written = json.loads(Path(entry["output_path"]).read_text())
        assert written["command"] == "search"
        assert written["input"]["query"] == entry["query"]
        assert written["data"]["results"][0]["raw_content"]


def test_search_batch_single_query_value_is_still_batch(capsys, monkeypatch, tmp_path, search_with_content_fixture):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setenv(cli._OUTPUT_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(cli, "_post_json", lambda path, body, key, timeout: (200, search_with_content_fixture))

    rc = cli.main(["search", "--query", "solo"])
    assert rc == 0
    status = json.loads(capsys.readouterr().out)
    assert status["output_mode"] == "batch"
    assert len(status["results"]) == 1


def test_search_batch_autonames_unique_files_for_colliding_slugs(
    capsys, monkeypatch, tmp_path, search_with_content_fixture
):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setenv(cli._OUTPUT_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(cli, "_post_json", lambda path, body, key, timeout: (200, search_with_content_fixture))

    # "a-b" and "a b" slugify to the same value; the second must get an index suffix.
    rc = cli.main(["search", "--query", "a-b", "--query", "a b"])
    assert rc == 0
    status = json.loads(capsys.readouterr().out)
    paths = [entry["output_path"] for entry in status["results"]]
    assert len(set(paths)) == 2
    assert all(Path(p).exists() for p in paths)


def test_search_batch_partial_failure(capsys, monkeypatch, tmp_path, search_with_content_fixture):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setenv(cli._OUTPUT_DIR_ENV, str(tmp_path))

    def fake_post(path, body, key, timeout):
        if body["query"] == "bad":
            return 500, None
        return 200, search_with_content_fixture

    monkeypatch.setattr(cli, "_post_json", fake_post)
    rc = cli.main(["search", "--query", "good", "--query", "bad", "--query", "also-good"])
    assert rc == 0  # partial failure is a success exit
    captured = capsys.readouterr()
    status = json.loads(captured.out)
    assert status["status"] == "partial"
    assert status["summary"]["success_count"] == 2
    assert status["summary"]["failed_count"] == 1
    assert status["summary"]["credits_used"] == 2 * search_with_content_fixture["creditsUsed"]
    bad = next(entry for entry in status["results"] if entry["query"] == "bad")
    assert bad["output_path"] is None
    assert bad["summary"] is None
    assert bad["error"]["http_status"] == 500
    assert "Warning" in captured.err
    assert "bad" in captured.err


def test_search_batch_all_fail_returns_mapped_code(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setenv(cli._OUTPUT_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(cli, "_post_json", lambda path, body, key, timeout: (429, {"error": "credits exhausted"}))

    rc = cli.main(["search", "--query", "a", "--query", "b"])
    assert rc == cli.EXIT_QUOTA_RATE
    captured = capsys.readouterr()
    status = json.loads(captured.out)
    assert status["status"] == "error"
    assert status["summary"]["success_count"] == 0
    assert all(entry["output_path"] is None and entry["error"] for entry in status["results"])
    err = json.loads([line for line in captured.err.splitlines() if line.strip()][-1])
    assert err["command"] == "search"
    assert err["http_status"] == 429


def test_search_batch_auth_failure_maps_to_10(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setenv(cli._OUTPUT_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(cli, "_post_json", lambda path, body, key, timeout: (401, None))
    assert cli.main(["search", "--query", "a"]) == cli.EXIT_AUTH


def test_search_batch_runs_queries_in_parallel(monkeypatch, tmp_path, search_with_content_fixture):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setenv(cli._OUTPUT_DIR_ENV, str(tmp_path))

    def slow_post(path, body, key, timeout):
        time.sleep(0.5)
        return 200, search_with_content_fixture

    monkeypatch.setattr(cli, "_post_json", slow_post)
    args = parse(["search", "--query", "a", "--query", "b", "--query", "c", "--query", "d", "--concurrency", "4"])
    start = time.monotonic()
    code, status, error = cli.run_search(args, "fc-test-key")
    elapsed = time.monotonic() - start
    assert code == 0, error
    assert status["summary"]["success_count"] == 4
    # 4 sequential calls would take ~2.0s; 4 workers should finish near one call time.
    assert elapsed < 1.5, f"batch did not run in parallel (elapsed {elapsed:.2f}s)"


def test_search_batch_serial_forces_sequential(monkeypatch, tmp_path, search_with_content_fixture):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setenv(cli._OUTPUT_DIR_ENV, str(tmp_path))

    def slow_post(path, body, key, timeout):
        time.sleep(0.4)
        return 200, search_with_content_fixture

    monkeypatch.setattr(cli, "_post_json", slow_post)
    # --serial must win over --concurrency 8 and run one query at a time.
    args = parse(["search", "--query", "a", "--query", "b", "--query", "c", "--serial", "--concurrency", "8"])
    start = time.monotonic()
    code, status, error = cli.run_search(args, "fc-test-key")
    elapsed = time.monotonic() - start
    assert code == 0, error
    assert status["input"]["concurrency"] == 1
    assert elapsed >= 1.2, f"--serial did not force sequential execution (elapsed {elapsed:.2f}s)"


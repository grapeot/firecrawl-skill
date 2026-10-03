"""Live integration tests against the Firecrawl API.

Opt-in: only run when RUN_FIRECRAWL_INTEGRATION=1 and a key is resolvable
(FIRECRAWL_API_KEY or ONEPASSWORD_FIRECRAWL_REFERENCE in the environment/.env).
Each test consumes a small number of credits.
"""
import os

import pytest

from firecrawl_skill import cli

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def api_key():
    if os.environ.get("RUN_FIRECRAWL_INTEGRATION") != "1":
        pytest.skip("integration tests are opt-in: set RUN_FIRECRAWL_INTEGRATION=1")
    cli.load_workspace_env()
    try:
        return cli._get_api_key()
    except cli.ApiError:
        pytest.skip("no Firecrawl API key available (set FIRECRAWL_API_KEY or ONEPASSWORD_FIRECRAWL_REFERENCE)")


def test_search_live(api_key):
    rc, payload, error = cli.run_search(
        cli.build_parser().parse_args(["search", "firecrawl web scraping", "--max-results", "2", "--raw-content", "markdown"]),
        api_key,
    )
    assert rc == 0, error
    data = payload["data"]
    assert data["result_count"] == 2
    assert data["credits_used"] is not None
    for item in data["results"]:
        assert item["url"].startswith("http")
        assert item["title"]
        assert isinstance(item["raw_content"], str) and len(item["raw_content"]) > 100


def test_search_live_raw_content_off(api_key):
    rc, payload, error = cli.run_search(
        cli.build_parser().parse_args(["search", "firecrawl", "--max-results", "3", "--raw-content", "off"]),
        api_key,
    )
    assert rc == 0, error
    for item in payload["data"]["results"]:
        assert "raw_content" not in item


def test_search_live_time_range(api_key):
    rc, payload, error = cli.run_search(
        cli.build_parser().parse_args(["search", "OpenAI", "--max-results", "3", "--time-range", "week"]),
        api_key,
    )
    assert rc == 0, error
    assert payload["data"]["result_count"] <= 3


def test_extract_live(api_key):
    rc, payload, error = cli.run_extract(
        cli.build_parser().parse_args(["extract", "https://example.com", "--query", "example domain"]),
        api_key,
    )
    assert rc == 0, error
    data = payload["data"]
    assert data["result_count"] == 1
    assert data["failed_count"] == 0
    assert data["credits_used"] is not None
    item = data["results"][0]
    assert "domain" in item["markdown"].lower()
    assert (item["metadata"] or {}).get("title") == "Example Domain"
    assert isinstance(item["highlights"], list)


def test_extract_live_bad_url_degrades_gracefully(api_key):
    rc, payload, error = cli.run_extract(
        cli.build_parser().parse_args(["extract", "https://this-domain-does-not-exist-zz123.example.invalid"]),
        api_key,
    )
    # A failing URL must not raise; either a hard failure code or an all-failed result.
    assert rc in (cli.EXIT_OK, cli.EXIT_NETWORK_SERVER, cli.EXIT_REJECTED)
    if payload is not None:
        assert payload["data"]["result_count"] == 0 or payload["data"]["failed_count"] == 1


def test_usage_live(api_key):
    """One read-only billing call. Consumes 0 Firecrawl credits."""
    rc, payload, error = cli.run_usage(
        cli.build_parser().parse_args(["usage"]),
        api_key,
    )
    assert rc == 0, error
    data = payload["data"]
    assert data["provider"] == "firecrawl"
    assert isinstance(data["remaining_credits"], int) and data["remaining_credits"] >= 0
    assert isinstance(data["plan_credits"], int) and data["plan_credits"] > 0
    assert data["credits_used_in_period"] == data["plan_credits"] - data["remaining_credits"]
    assert data["billing_period_start"] and data["billing_period_end"]
    # raw carries the upstream body verbatim, so the normalized fields above can be
    # audited against it.
    assert data["raw"]["credit_usage"]["data"]["remainingCredits"] == data["remaining_credits"]
    assert data["periods"] == []

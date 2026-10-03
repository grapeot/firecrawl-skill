import json
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def search_with_content_fixture():
    return load_fixture("search_web_with_content.json")


@pytest.fixture
def search_news_fixture():
    return load_fixture("search_news.json")


@pytest.fixture
def scrape_ok_fixture():
    return load_fixture("scrape_ok.json")


@pytest.fixture
def scrape_highlights_fixture():
    return load_fixture("scrape_with_highlights.json")


@pytest.fixture
def credit_usage_fixture():
    return load_fixture("credit_usage_ok.json")


@pytest.fixture
def credit_usage_historical_fixture():
    return load_fixture("credit_usage_historical_ok.json")


@pytest.fixture
def credit_usage_not_found_fixture():
    return load_fixture("credit_usage_not_found.json")


@pytest.fixture
def live_usage_pair(credit_usage_fixture, credit_usage_historical_fixture):
    """The two billing fixtures as the live API actually returns them together.

    The current period recorded in `credit_usage_ok.json` matches the open-ended
    last entry of the historical fixture (`end_date: null`, same `start_date`), which
    is what `usage --history` has to dedupe in practice.
    """
    historical = json.loads(json.dumps(credit_usage_historical_fixture))
    current = credit_usage_fixture["data"]
    historical["periods"][-1] = {
        "startDate": current["billingPeriodStart"],
        "endDate": None,
        "creditsUsed": historical["periods"][-1]["creditsUsed"],
    }
    # Upstream's byApiKey=true response carries a key name per period; the default
    # byApiKey=false shape (what the CLI requests) omits the field entirely.
    historical["periods"][0]["apiKey"] = "Default"
    return credit_usage_fixture, historical

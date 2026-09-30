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

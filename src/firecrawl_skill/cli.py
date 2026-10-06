#!/usr/bin/env python3
"""firecrawl_skill CLI.

Agent-facing web search, URL extraction, and account credit-usage reporting over the
Firecrawl v2 API. The search/extract command surface and output contract mirror
tavily-skill; see docs/rfc.md.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import math
import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API_BASE = "https://api.firecrawl.dev/v2"

DEFAULT_MAX_RESULTS = 6
DEFAULT_TIMEOUT = 60
DEFAULT_RAW_CONTENT = "markdown"
DEFAULT_TOPIC = "general"
DEFAULT_SEARCH_DEPTH = "advanced"
DEFAULT_EXTRACT_DEPTH = "advanced"
DEFAULT_EXTRACT_FORMAT = "markdown"
DEFAULT_CONCURRENCY = 4
MAX_RESULTS_LIMIT = 20
MAX_URLS_LIMIT = 20
MAX_USAGE_PERIODS = 100

# Upstream billing endpoints. `usage` reads the first; the historical endpoint is the
# only upstream source of per-period consumption (`--history`). The path is
# `/v2/team/credit-usage`, not `/v2/credit-usage` — see docs/rfc.md (D5).
USAGE_PATH = "/team/credit-usage"
USAGE_HISTORICAL_PATH = "/team/credit-usage/historical"

SEARCH_DEPTH_CHOICES = ["basic", "advanced", "fast", "ultra-fast"]
TOPIC_CHOICES = ["general", "news", "finance"]
TIME_RANGE_CHOICES = ["day", "week", "month", "year"]
RAW_CONTENT_CHOICES = ["off", "markdown", "text"]
EXTRACT_DEPTH_CHOICES = ["basic", "advanced"]
EXTRACT_FORMAT_CHOICES = ["markdown", "text"]

TIME_RANGE_TBS = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}

# Exit codes (contract — see README and AGENTS.md).
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_AUTH = 10
EXIT_QUOTA_RATE = 11
EXIT_REJECTED = 12
EXIT_NETWORK_SERVER = 13

# Optional 1Password reference, e.g. op://Vault/Item/field — never commit real vault paths.
_ONEPASSWORD_REF_ENV = "ONEPASSWORD_FIRECRAWL_REFERENCE"
_OUTPUT_DIR_ENV = "FIRECRAWL_CLI_OUTPUT_DIR"


class ApiError(Exception):
    def __init__(self, message: str, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


# ---------------------------------------------------------------------------
# Environment and key resolution
# ---------------------------------------------------------------------------


def get_default_output_dir() -> Path:
    raw = os.environ.get(_OUTPUT_DIR_ENV)
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.cwd() / "tmp" / "firecrawl"


def _load_env_file(env_file: Path) -> bool:
    """Minimal stdlib .env loader: KEY=VALUE lines, no override of set vars."""
    if not env_file.exists():
        return False
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
    return True


def load_workspace_env(explicit_env_file: str | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit_env_file:
        candidates.append(Path(explicit_env_file).expanduser().resolve())

    cwd = Path.cwd()
    candidates.extend((parent / ".env") for parent in [cwd] + list(cwd.parents))

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if _load_env_file(candidate):
            return candidate
    return None


def _get_api_key() -> str:
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if api_key:
        return api_key

    reference = os.environ.get(_ONEPASSWORD_REF_ENV)
    if reference:
        try:
            result = subprocess.run(
                ["op", "read", reference],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            raise ApiError(
                "Firecrawl API key not found. Set FIRECRAWL_API_KEY, or fix "
                f"{_ONEPASSWORD_REF_ENV} and ensure the 1Password CLI is logged in."
            )
        if result.returncode == 0:
            key = result.stdout.strip()
            if key:
                return key
        raise ApiError(
            "Firecrawl API key not found. Set FIRECRAWL_API_KEY, or fix "
            f"{_ONEPASSWORD_REF_ENV} and ensure the 1Password CLI is logged in."
        )

    raise ApiError(
        "Firecrawl API key not found. Set FIRECRAWL_API_KEY, or set "
        f"{_ONEPASSWORD_REF_ENV} to an `op read`-compatible secret reference "
        "and ensure the 1Password CLI is logged in."
    )


# ---------------------------------------------------------------------------
# Argument parsing and validation
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Firecrawl search CLI")
    parser.add_argument("--env-file", help="Optional .env file path")

    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser("search", help="Search the web with Firecrawl")
    search_parser.add_argument(
        "query",
        nargs="?",
        help="Search query (single-query mode; mutually exclusive with --query)",
    )
    search_parser.add_argument(
        "--query",
        dest="queries",
        action="append",
        default=[],
        metavar="QUERY",
        help=(
            "One complete query string; repeat for a multi-query batch executed "
            "in parallel (mutually exclusive with the positional query)"
        ),
    )
    search_parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Batch mode: parallel queries (default: {DEFAULT_CONCURRENCY})",
    )
    search_parser.add_argument(
        "--serial",
        action="store_true",
        help="Batch mode: force sequential execution (equivalent to --concurrency 1; overrides --concurrency)",
    )
    search_parser.add_argument(
        "--max-results",
        type=int,
        default=DEFAULT_MAX_RESULTS,
        help=f"Maximum number of results (default: {DEFAULT_MAX_RESULTS})",
    )
    search_parser.add_argument(
        "--search-depth",
        choices=SEARCH_DEPTH_CHOICES,
        default=None,
        help=(
            f"Search depth (accepted for tavily-skill compatibility; Firecrawl has a "
            f"single search mode and this value is ignored; default: {DEFAULT_SEARCH_DEPTH})"
        ),
    )
    search_parser.add_argument(
        "--topic",
        choices=TOPIC_CHOICES,
        default=DEFAULT_TOPIC,
        help=f"Search topic (default: {DEFAULT_TOPIC}); 'finance' is not supported",
    )
    search_parser.add_argument(
        "--time-range",
        choices=TIME_RANGE_CHOICES,
        help="Only include results from a recent time range",
    )
    search_parser.add_argument("--start-date", help="Return results on or after YYYY-MM-DD")
    search_parser.add_argument("--end-date", help="Return results on or before YYYY-MM-DD")
    search_parser.add_argument(
        "--include-domain",
        dest="include_domains",
        action="append",
        default=[],
        help="Restrict results to a domain; repeat for multiple domains (mutually exclusive with --exclude-domain)",
    )
    search_parser.add_argument(
        "--exclude-domain",
        dest="exclude_domains",
        action="append",
        default=[],
        help="Exclude a domain; repeat for multiple domains (mutually exclusive with --include-domain)",
    )
    search_parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print the full JSON payload to stdout instead of writing it to the default output file",
    )
    search_parser.add_argument(
        "--raw-content",
        choices=RAW_CONTENT_CHOICES,
        default=DEFAULT_RAW_CONTENT,
        help=f"Include raw page content in each result (default: {DEFAULT_RAW_CONTENT})",
    )
    search_parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Request timeout in seconds (default: {DEFAULT_TIMEOUT})",
    )
    search_parser.add_argument(
        "--country",
        help="Optional geo-target, for example 'US' or 'Germany'",
    )
    search_parser.add_argument(
        "--output",
        help="Also write the JSON payload to a file path",
    )
    search_parser.set_defaults(include_images=False, include_image_descriptions=False)
    search_parser.add_argument(
        "--images",
        dest="include_images",
        action="store_true",
        help="Enable image results (limit applies per source)",
    )
    search_parser.add_argument(
        "--no-images",
        dest="include_images",
        action="store_false",
        help="Disable image results",
    )
    search_parser.add_argument(
        "--image-descriptions",
        dest="include_image_descriptions",
        action="store_true",
        help="Not supported by Firecrawl (image results have no description field)",
    )

    extract_parser = subparsers.add_parser("extract", help="Extract content from URLs with Firecrawl")
    extract_parser.add_argument("urls", nargs="+", help="One or more URLs to extract")
    extract_parser.add_argument(
        "--extract-depth",
        choices=EXTRACT_DEPTH_CHOICES,
        default=None,
        help=(
            f"Extraction depth (accepted for tavily-skill compatibility; Firecrawl always "
            f"fully renders pages and this value is ignored; default: {DEFAULT_EXTRACT_DEPTH})"
        ),
    )
    extract_parser.add_argument(
        "--format",
        choices=EXTRACT_FORMAT_CHOICES,
        default=DEFAULT_EXTRACT_FORMAT,
        help=f"Extracted content format (default: {DEFAULT_EXTRACT_FORMAT})",
    )
    extract_parser.add_argument(
        "--query",
        help="Optional query to request query-relevant highlights for each page",
    )
    extract_parser.add_argument(
        "--chunks-per-source",
        type=int,
        help="Accepted for compatibility; Firecrawl highlights have no chunk-count control and this is ignored",
    )
    extract_parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Per-URL request timeout in seconds (default: {DEFAULT_TIMEOUT})",
    )
    extract_parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print the full JSON payload to stdout instead of writing it to the default output file",
    )
    extract_parser.add_argument(
        "--output",
        help="Write the full extract payload to a file and return status JSON on stdout",
    )
    extract_parser.set_defaults(include_images=False, include_favicon=False)
    extract_parser.add_argument(
        "--images",
        dest="include_images",
        action="store_true",
        help="Enable image extraction",
    )
    extract_parser.add_argument(
        "--no-images",
        dest="include_images",
        action="store_false",
        help="Disable image extraction",
    )
    extract_parser.add_argument(
        "--favicon",
        dest="include_favicon",
        action="store_true",
        help="Include favicon URLs from page metadata when present",
    )

    usage_parser = subparsers.add_parser(
        "usage", help="Report Firecrawl account credit usage (no credits consumed)"
    )
    usage_parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Request timeout in seconds (default: {DEFAULT_TIMEOUT})",
    )
    usage_parser.add_argument(
        "--history",
        type=int,
        default=0,
        metavar="N",
        help=(
            f"Include the N most recent billing periods of credit consumption "
            f"(1-{MAX_USAGE_PERIODS}, default 0 = current period only)"
        ),
    )
    usage_parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print the full JSON payload to stdout instead of writing it to the default output file",
    )
    usage_parser.add_argument(
        "--output",
        help="Write the full usage payload to a file and return status JSON on stdout",
    )

    return parser


def _parse_iso_date(value: str, flag: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{flag} must be a valid YYYY-MM-DD date, got {value!r}") from exc


def _build_tbs(args: argparse.Namespace) -> str | None:
    if args.time_range:
        return TIME_RANGE_TBS[args.time_range]
    if args.start_date or args.end_date:
        start = _parse_iso_date(args.start_date, "--start-date")
        end = _parse_iso_date(args.end_date, "--end-date")
        return f"cdr:1,cd_min:{start.month:02d}/{start.day:02d}/{start.year},cd_max:{end.month:02d}/{end.day:02d}/{end.year}"
    return None


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.command == "usage":
        if args.timeout <= 0:
            parser.error("--timeout must be greater than 0.")
        if args.stdout and args.output:
            parser.error("Use either --stdout or --output, not both.")
        if not 0 <= args.history <= MAX_USAGE_PERIODS:
            parser.error(f"--history must be between 0 and {MAX_USAGE_PERIODS} (0 = current period only).")
        return

    if args.command == "extract":
        if args.timeout <= 0:
            parser.error("--timeout must be greater than 0.")
        if args.stdout and args.output:
            parser.error("Use either --stdout or --output, not both.")
        if not 1 <= len(args.urls) <= MAX_URLS_LIMIT:
            parser.error(f"extract accepts between 1 and {MAX_URLS_LIMIT} URLs.")
        if args.chunks_per_source is not None and args.chunks_per_source <= 0:
            parser.error("--chunks-per-source must be greater than 0.")
        if args.chunks_per_source is not None and not args.query:
            parser.error("--chunks-per-source requires --query.")
        return

    has_positional_query = args.query is not None
    has_batch_queries = bool(args.queries)
    if has_positional_query and has_batch_queries:
        parser.error("Provide either a positional query or --query, not both.")
    if not has_positional_query and not has_batch_queries:
        parser.error("Provide a search query: a positional query, or one or more --query values.")
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1.")
    if has_batch_queries and args.stdout:
        parser.error("--stdout is not valid in batch mode; batch mode always writes one file per query.")
    if has_batch_queries and args.output:
        parser.error("--output is not valid in batch mode; batch mode always writes one file per query.")

    if args.time_range and (args.start_date or args.end_date):
        parser.error("Use either --time-range or --start-date/--end-date, not both.")
    if bool(args.start_date) != bool(args.end_date):
        parser.error("--start-date and --end-date must be used together.")
    if args.start_date or args.end_date:
        try:
            _build_tbs(args)  # validates date format early
        except argparse.ArgumentTypeError as exc:
            parser.error(str(exc))
    if args.stdout and args.output:
        parser.error("Use either --stdout or --output, not both.")
    if not 1 <= args.max_results <= MAX_RESULTS_LIMIT:
        parser.error(f"--max-results must be between 1 and {MAX_RESULTS_LIMIT}.")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than 0.")
    if args.include_domains and args.exclude_domains:
        parser.error(
            "Use either --include-domain or --exclude-domain, not both "
            "(Firecrawl domain filters are mutually exclusive)."
        )
    if args.topic == "finance":
        parser.error(
            "--topic finance is not supported by Firecrawl. "
            "Use --include-domain to restrict results to finance domains instead."
        )
    if args.include_image_descriptions:
        parser.error(
            "--image-descriptions is not supported by Firecrawl "
            "(image results carry URLs and dimensions only)."
        )


# ---------------------------------------------------------------------------
# Request building
# ---------------------------------------------------------------------------


def _normalize_domain(domain: str) -> str:
    return domain.lower().split("://", 1)[-1].split("/", 1)[0]


def _is_batch(args: argparse.Namespace) -> bool:
    """Batch mode is selected solely by one or more `--query` values."""
    return bool(getattr(args, "queries", None))


def _with_query(args: argparse.Namespace, query: str) -> argparse.Namespace:
    """Per-query copy so parallel workers never share a mutable `query` field."""
    local = argparse.Namespace(**vars(args))
    local.query = query
    return local


def _build_search_request(args: argparse.Namespace) -> dict[str, Any]:
    sources: list[str] = ["news"] if args.topic == "news" else ["web"]
    if args.include_images:
        sources.append("images")

    request: dict[str, Any] = {
        "query": args.query,
        "limit": args.max_results,
        "sources": sources,
        "timeout": args.timeout * 1000,
    }
    if args.raw_content != "off":
        request["scrapeOptions"] = {"formats": ["markdown"]}

    tbs = _build_tbs(args)
    if tbs:
        request["tbs"] = tbs
    if args.include_domains:
        request["includeDomains"] = [_normalize_domain(d) for d in args.include_domains]
    elif args.exclude_domains:
        request["excludeDomains"] = [_normalize_domain(d) for d in args.exclude_domains]
    if args.country:
        request["location"] = args.country
        if re.fullmatch(r"[A-Z]{2}", args.country):
            request["country"] = args.country
    return request


def _build_extract_formats(args: argparse.Namespace) -> list[Any]:
    formats: list[Any] = ["markdown"]
    if args.include_images:
        formats.append("images")
    if args.query:
        formats.append({"type": "highlights", "query": args.query})
    return formats


def _build_extract_request(args: argparse.Namespace, url: str) -> dict[str, Any]:
    return {
        "url": url,
        "formats": _build_extract_formats(args),
        "timeout": args.timeout * 1000,
    }


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def _post_json(path: str, body: dict[str, Any], api_key: str, timeout: float) -> tuple[int | None, dict[str, Any] | None]:
    """POST JSON to the Firecrawl API. Returns (http_status, parsed_body).

    http_status is None on network-level failure; parsed_body is None when the
    response body was not valid JSON.
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        API_BASE + path,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read().decode("utf-8")
        except UnicodeDecodeError:
            raw = ""
        return status, _parse_json_dict(raw)
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError):
        return None, None

    return status, _parse_json_dict(raw)


def _parse_json_dict(raw: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _get_json(
    path: str, api_key: str, timeout: float, params: dict[str, str] | None = None
) -> tuple[int | None, dict[str, Any] | None]:
    """GET JSON from the Firecrawl API. Returns (http_status, parsed_body).

    Same contract as `_post_json`: http_status is None on network-level failure,
    parsed_body is None when the response body was not a JSON object. The billing
    endpoints need no request body, so params are the only query input.
    """
    url = API_BASE + path
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _parse_json_dict(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8")
        except UnicodeDecodeError:
            raw = ""
        return exc.code, _parse_json_dict(raw)
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError):
        return None, None


def _exit_code_for(status: int | None, payload: dict[str, Any] | None) -> int:
    if status is None:
        return EXIT_NETWORK_SERVER
    if status == 200:
        return EXIT_OK if (payload or {}).get("success") else EXIT_REJECTED
    if status in (401, 403):
        return EXIT_AUTH
    if status in (402, 429):
        return EXIT_QUOTA_RATE
    if status in (408, 500, 502, 503, 504):
        return EXIT_NETWORK_SERVER
    return EXIT_REJECTED


def _error_message(status: int | None, payload: dict[str, Any] | None) -> str:
    if status is None:
        return "network error (timeout or connection failure)"
    if isinstance(payload, dict) and payload.get("error"):
        return str(payload["error"])
    if status == 200:
        return "upstream returned success=false or an unparseable response"
    return f"HTTP {status}"


def _report_failure(command: str, code: int, error: dict[str, Any]) -> None:
    print(
        json.dumps({"command": command, "http_status": error.get("http_status"), "error": error.get("error")},
                   ensure_ascii=False),
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _normalize_search_response(args: argparse.Namespace, response: dict[str, Any]) -> dict[str, Any]:
    data = response.get("data") or {}
    web = data.get("web") or []
    news = data.get("news") or []
    images = data.get("images") or []

    results: list[dict[str, Any]]
    if args.topic == "news":
        results = []
        for item in news:
            item = dict(item)
            if not item.get("description") and item.get("snippet"):
                item["description"] = item["snippet"]
            results.append(item)
    else:
        results = [dict(item) for item in web]

    if args.raw_content != "off":
        for item in results:
            item["raw_content"] = item.pop("markdown", None)
    else:
        for item in results:
            item.pop("markdown", None)

    search_depth = args.search_depth or DEFAULT_SEARCH_DEPTH
    return {
        "command": "search",
        "input": {
            "query": args.query,
            "max_results": args.max_results,
            "search_depth": search_depth,
            "topic": args.topic,
            "time_range": args.time_range,
            "start_date": args.start_date,
            "end_date": args.end_date,
            "include_domains": args.include_domains,
            "exclude_domains": args.exclude_domains,
            "include_images": args.include_images,
            "raw_content": args.raw_content,
            "country": args.country,
            "timeout": args.timeout,
        },
        "data": {
            "query": args.query,
            "results": results,
            "images": images,
            "news": news,
            "credits_used": response.get("creditsUsed"),
            "cost_breakdown": _search_cost_breakdown(results, response.get("creditsUsed")),
            "job_id": response.get("id"),
            "result_count": len(results),
            "image_count": len(images),
        },
    }


def _search_cost_breakdown(
    results: list[dict[str, Any]], reported: Any
) -> dict[str, Any]:
    base = _base_search_credits(len(results))
    documents = _cost_documents(results, "raw_content")
    reported_total = int(reported) if isinstance(reported, (int, float)) else None
    return _build_cost_breakdown(base, documents, reported_total)


def _base_search_credits(result_count: int) -> int:
    """Search base cost: 2 credits per 10 results actually returned, rounded up. A search
    that returns nothing bills 0 base. Verified against 212 recorded calls (2026-10).

    The base keys off returned results, not the requested `--max-results`: a query that
    asks for 6 but matches 0 pays nothing."""
    return 2 * math.ceil(result_count / 10)


def _normalize_highlights(value: Any) -> list[str]:
    """Firecrawl returns highlights as a string, list, or null; normalize to a list."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return [str(value)]


def _scrape_credits_used(response: dict[str, Any]) -> int | None:
    data = response.get("data") or {}
    metadata = data.get("metadata") or {}
    for candidate in (response.get("creditsUsed"), metadata.get("creditsUsed"), metadata.get("creditCount")):
        if isinstance(candidate, (int, float)):
            return int(candidate)
    return None


# ---------------------------------------------------------------------------
# Cost breakdown (observability)
# ---------------------------------------------------------------------------
#
# Firecrawl search = base (2 credits / 10 results, rounded up) + per-document scrape
# cost. Per-document cost is not linear: HTML is 1 credit, x.com/twitter.com go through
# the Grok API at 30 credits each, and PDFs bill 1 credit per page. The upstream
# `creditsUsed` is authoritative; this breakdown reconstructs *why* it is what it is so
# a finished payload can be audited after the fact. It is best-effort only and never
# blocks: when the reconstruction disagrees with the reported total it records a warning
# and moves on. See docs/rfc.md (D7).

_X_HOSTS = ("x.com", "twitter.com")
_PDF_CONTENT_TYPE = "application/pdf"


def _host_of(url: Any) -> str:
    try:
        host = (urllib.parse.urlsplit(str(url)).hostname or "").lower()
    except (ValueError, TypeError):
        return ""
    return host.rstrip(".")  # normalize FQDN trailing-dot form (x.com. -> x.com)


def _is_x_host(host: str) -> bool:
    return any(host == base or host.endswith("." + base) for base in _X_HOSTS)


def _as_int(value: Any) -> int | None:
    """Numeric fields upstream returns as int/float; bool is rejected (JSON true/false
    must not be read as 1/0 in a billing figure)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _document_kind(url: Any, metadata: Any) -> str:
    host = _host_of(url)
    if _is_x_host(host):
        return "x"
    if isinstance(metadata, dict):
        content_type = str(metadata.get("contentType") or "")
        if content_type.startswith(_PDF_CONTENT_TYPE):
            return "pdf"
    return "html"


def _document_cost(url: Any, metadata: Any, credits: int | None) -> tuple[str, int | None, dict[str, Any]]:
    """Return (kind, credits, extra) for one returned document.

    credits is None when upstream omitted the per-document figure; it is then inferred
    from the kind (X = 30, PDF = page count when known, else 1). Inference only feeds the
    breakdown, never the authoritative `credits_used`.
    """
    kind = _document_kind(url, metadata)
    extra: dict[str, Any] = {}
    pages: int | None = _as_int(metadata.get("numPages")) if isinstance(metadata, dict) else None
    if kind == "pdf" and pages is not None:
        extra["pages"] = pages
    if credits is None:
        if kind == "x":
            credits = 30
        elif kind == "pdf" and pages is not None:
            credits = pages
        else:
            credits = 1
    return kind, credits, extra


def _build_cost_breakdown(
    base: int,
    documents: list[dict[str, Any]],
    reported: int | None,
) -> dict[str, Any]:
    priced = [d for d in documents if _as_int(d.get("credits")) is not None]
    modelled = base + sum(_as_int(d["credits"]) or 0 for d in priced)
    reconciles: bool | None
    warning: str | None
    if reported is None:
        reconciles = None
        warning = None
    elif modelled == reported:
        reconciles = True
        warning = None
    else:
        reconciles = False
        warning = (
            f"cost breakdown modelled {modelled} credits (base {base} + "
            f"{len(priced)} priced document(s)) but the API reported {reported}; "
            "the payload is unaffected — treat credits_used as authoritative."
        )
    return {
        "base": base,
        "documents": documents,
        "modelled_total": modelled,
        "reported_total": reported,
        "reconciles": reconciles,
        "warning": warning,
    }


def _metadata_credits(metadata: Any) -> int | None:
    """The per-document charge upstream reports, or None when it is absent."""
    if isinstance(metadata, dict):
        return _as_int(metadata.get("creditsUsed", metadata.get("creditCount")))
    return None


def _cost_documents(results: list[dict[str, Any]], content_key: str) -> list[dict[str, Any]]:
    """Per-document cost rows for a set of normalized results.

    Firecrawl bills per *returned document*: for search the document is `raw_content`,
    for extract it is `markdown`. No document => 0 credits, even if metadata came back.
    """
    documents: list[dict[str, Any]] = []
    for item in results:
        metadata = item.get("metadata")
        if isinstance(item.get(content_key), str):
            credits: int | None = _metadata_credits(metadata)
        else:
            credits = 0
        kind, credits, extra = _document_cost(item.get("url"), metadata, credits)
        entry: dict[str, Any] = {"url": item.get("url"), "kind": kind, "credits": credits}
        entry.update(extra)
        documents.append(entry)
    return documents


def _normalize_extract_result(args: argparse.Namespace, url: str, response: dict[str, Any]) -> dict[str, Any]:
    data = response.get("data") or {}
    item: dict[str, Any] = {
        "url": url,
        "markdown": data.get("markdown"),
        "metadata": data.get("metadata"),
        "error": None,
    }
    if args.query:
        item["highlights"] = _normalize_highlights(data.get("highlights"))
    if args.include_images:
        item["images"] = data.get("images") or []
    if not args.include_favicon and isinstance(item.get("metadata"), dict) and "favicon" in item["metadata"]:
        item["metadata"] = {k: v for k, v in item["metadata"].items() if k != "favicon"}
    return item


# ---------------------------------------------------------------------------
# Normalization (usage)
# ---------------------------------------------------------------------------


def _int_or_none(value: Any) -> int | None:
    """Upstream credit fields are numbers; keep them as ints, null when absent."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _normalize_period(item: dict[str, Any]) -> dict[str, Any]:
    """One historical billing period.

    Upstream returns `creditsUsed` in the live response; the published OpenAPI
    example calls the same value `totalCredits`. Both names are read, first
    present wins, so a spec rename does not silently null the field.

    `apiKey` is deliberately not projected: the CLI never requests `byApiKey=true`,
    and `input` records the request as `by_api_key: false`, so a key name appearing
    here would contradict the recorded request. It stays visible in `data.raw`.
    """
    return {
        "start_date": item.get("startDate"),
        "end_date": item.get("endDate"),
        "credits_used": _int_or_none(item.get("creditsUsed", item.get("totalCredits"))),
    }


def _normalize_usage_response(
    args: argparse.Namespace, response: dict[str, Any], historical: dict[str, Any] | None
) -> dict[str, Any]:
    raw_data = response.get("data")
    data = raw_data if isinstance(raw_data, dict) else {}
    remaining = _int_or_none(data.get("remainingCredits"))
    plan = _int_or_none(data.get("planCredits"))
    used = None if remaining is None or plan is None else plan - remaining

    input_block: dict[str, Any] = {
        "timeout": args.timeout,
        "history": args.history,
        "stdout": args.stdout,
        "output": args.output,
    }
    if args.history:
        # The historical endpoint is the only second call, and it is always made with
        # byApiKey left at its default — recorded so `periods` shape is explainable.
        input_block["by_api_key"] = False
    data_block: dict[str, Any] = {
        "provider": "firecrawl",
        "remaining_credits": remaining,
        "plan_credits": plan,
        "credits_used_in_period": used,
        "billing_period_start": data.get("billingPeriodStart"),
        "billing_period_end": data.get("billingPeriodEnd"),
        "periods": [],
        # Normalized top-level fields only carry values the API returns; `raw` keeps
        # the full upstream bodies verbatim so nothing is invented here.
        "raw": {"credit_usage": response},
    }

    if historical is not None:
        periods = historical.get("periods") or []
        if isinstance(periods, list):
            data_block["periods"] = [_normalize_period(p) for p in periods if isinstance(p, dict)]
        data_block["raw"]["credit_usage_historical"] = historical

    return {"command": "usage", "input": input_block, "data": data_block}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _payload_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """The lightweight per-payload summary shared by single- and batch-mode status objects."""
    data = payload.get("data", {})
    return {
        "result_count": data.get("result_count"),
        "failed_count": data.get("failed_count"),
        "image_count": data.get("image_count"),
        "credits_used": data.get("credits_used"),
        "remaining_credits": data.get("remaining_credits"),
    }


def _write_payload_file(payload: dict[str, Any], target: Path) -> int:
    """Write the full payload as pretty JSON; returns the UTF-8 byte length."""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def _emit_payload(payload: dict[str, Any], output_path: str | None) -> None:
    if not output_path:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    target = Path(output_path).expanduser()
    payload_bytes = _write_payload_file(payload, target)

    command = payload.get("command")
    status_payload = {
        "command": command,
        "status": "ok",
        "output_mode": "file",
        "output_path": str(target),
        "payload_bytes": payload_bytes,
        "summary": _payload_summary(payload),
        "payload_schema": _payload_schema(command),
    }
    print(json.dumps(status_payload, ensure_ascii=False, indent=2))
    print(f"Saved JSON to {target}", file=sys.stderr)


def _slugify(value: str, max_length: int = 48) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
    if not slug:
        return "payload"
    return slug[:max_length].rstrip("_") or "payload"


def _default_output_path(args: argparse.Namespace) -> str:
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.command == "search":
        seed = args.query
    elif args.command == "usage":
        seed = "credits"
    else:
        seed = args.urls[0]
    slug = _slugify(seed)
    return str(get_default_output_dir() / f"{args.command}_{timestamp}_{slug}.json")


def _resolve_output_path(args: argparse.Namespace) -> str | None:
    if getattr(args, "stdout", False):
        return None
    if getattr(args, "output", None):
        return args.output
    return _default_output_path(args)


def _batch_output_paths(queries: list[str]) -> list[str]:
    """One output path per query under the default output dir.

    Shares the `{command}_{timestamp}_{slug}.json` scheme with single mode. One batch
    shares a timestamp; a slug collision (two queries reducing to the same slug, or an
    existing corpus file) appends an index so every query keeps its own file.
    """
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = get_default_output_dir()
    used: set[str] = set()
    paths: list[str] = []
    for query in queries:
        base = f"search_{timestamp}_{_slugify(query)}"
        candidate = f"{base}.json"
        suffix = 1
        while candidate in used or (out_dir / candidate).exists():
            candidate = f"{base}_{suffix}.json"
            suffix += 1
        used.add(candidate)
        paths.append(str(out_dir / candidate))
    return paths


def _payload_schema(command: object) -> dict[str, Any]:
    base = {
        "command": "string",
        "input": "object",
    }
    if command == "extract":
        return {
            **base,
            "data": {
                "results": "array",
                "failed_results": "array",
                "credits_used": "number|null",
                "cost_breakdown": "object",
                "result_count": "number",
                "failed_count": "number",
                "image_count": "number",
            },
        }
    if command == "usage":
        return {
            **base,
            "data": {
                "provider": "string",
                "remaining_credits": "number|null",
                "plan_credits": "number|null",
                "credits_used_in_period": "number|null",
                "billing_period_start": "string|null",
                "billing_period_end": "string|null",
                "periods": "array",
                "raw": "object",
            },
        }
    return {
        **base,
        "data": {
            "query": "string",
            "results": "array",
            "images": "array",
            "news": "array",
            "credits_used": "number|null",
            "cost_breakdown": "object",
            "job_id": "string|null",
            "result_count": "number",
            "image_count": "number",
        },
    }


def _estimate_search_credits(args: argparse.Namespace) -> int:
    search_credits = 2 * math.ceil(args.max_results / 10)
    scrape_credits = args.max_results if args.raw_content != "off" else 0
    return search_credits + scrape_credits


def _estimate_extract_credits(args: argparse.Namespace) -> int:
    per_page = 1
    if args.query:
        per_page += 4  # highlights format
    return len(args.urls) * per_page


def _print_credit_estimate(command: str, estimate: int) -> None:
    if command == "usage":
        # R7 applies to credit-consuming calls only; usage reads a billing endpoint
        # that costs nothing, and printing an estimate here would imply otherwise.
        return
    print(f"Estimated Firecrawl credits: {estimate}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _print_search_notes(args: argparse.Namespace) -> None:
    if args.search_depth:
        print(
            f"Note: --search-depth {args.search_depth!r} is ignored (Firecrawl has a single search mode).",
            file=sys.stderr,
        )
    if args.raw_content == "text":
        print(
            "Note: --raw-content text maps to markdown (Firecrawl has no text tier).",
            file=sys.stderr,
        )


def _run_single_search(
    args: argparse.Namespace, query: str, api_key: str
) -> tuple[int, dict[str, Any] | None, dict[str, Any] | None]:
    local = _with_query(args, query)
    request = _build_search_request(local)
    status, payload = _post_json("/search", request, api_key, local.timeout)
    code = _exit_code_for(status, payload)
    if code != EXIT_OK:
        return code, None, {"http_status": status, "error": _error_message(status, payload)}
    return EXIT_OK, _normalize_search_response(local, payload or {}), None


def run_search(
    args: argparse.Namespace, api_key: str
) -> tuple[int, dict[str, Any] | None, dict[str, Any] | None]:
    if _is_batch(args):
        return run_search_batch(args, api_key)
    _print_credit_estimate("search", _estimate_search_credits(args))
    _print_search_notes(args)
    return _run_single_search(args, args.query, api_key)


def run_search_batch(
    args: argparse.Namespace, api_key: str
) -> tuple[int, dict[str, Any] | None, dict[str, Any] | None]:
    """Run one query per `--query` value, in parallel by default.

    Each query gets its own full-payload file, preserving the one-query-one-file corpus
    invariant. The returned status object is the only thing printed to stdout: one
    entry per query plus aggregate counts and summed credits. The exit code is 0 when
    at least one query succeeds (partial failure included); when every query fails it
    is the first failure's mapped code, mirroring `extract`'s all-failed behavior.
    """
    queries = args.queries
    per_query = _estimate_search_credits(args)
    print(f"Estimated Firecrawl credits: {per_query * len(queries)}", file=sys.stderr)
    _print_search_notes(args)

    concurrency = 1 if args.serial else args.concurrency
    paths = _batch_output_paths(queries)

    def work(index: int) -> tuple[int, dict[str, Any] | None, dict[str, Any] | None]:
        try:
            return _run_single_search(args, queries[index], api_key)
        except Exception as exc:  # one bad query must never abort the batch
            return EXIT_REJECTED, None, {"http_status": None, "error": f"unexpected error: {exc}"}

    outcomes: list[tuple[int, dict[str, Any] | None, dict[str, Any] | None] | None] = [None] * len(queries)
    if concurrency <= 1:
        for index in range(len(queries)):
            outcomes[index] = work(index)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            future_to_index = {executor.submit(work, index): index for index in range(len(queries))}
            for future in concurrent.futures.as_completed(future_to_index):
                outcomes[future_to_index[future]] = future.result()

    entries: list[dict[str, Any]] = []
    success_count = 0
    credits_total = 0
    has_credits = False
    first_error: tuple[int, dict[str, Any]] | None = None

    for index, outcome in enumerate(outcomes):
        code, payload, error = outcome or (EXIT_REJECTED, None, None)
        if code == EXIT_OK and payload is not None:
            target = Path(paths[index])
            _write_payload_file(payload, target)
            summary = _payload_summary(payload)
            entries.append(
                {"query": queries[index], "output_path": str(target), "summary": summary, "error": None}
            )
            success_count += 1
            credits = summary.get("credits_used")
            if isinstance(credits, int) and not isinstance(credits, bool):
                credits_total += credits
                has_credits = True
        else:
            failure = error or {"http_status": None, "error": "request failed"}
            entries.append(
                {"query": queries[index], "output_path": None, "summary": None, "error": failure}
            )
            print(f"Warning: search failed for {queries[index]!r}: {failure.get('error')}", file=sys.stderr)
            if first_error is None:
                first_error = (code, failure)

    failed_count = len(queries) - success_count
    batch_status: dict[str, Any] = {
        "command": "search",
        "status": "ok" if failed_count == 0 else ("error" if success_count == 0 else "partial"),
        "output_mode": "batch",
        "output_dir": str(get_default_output_dir()),
        "input": {
            "queries": queries,
            "concurrency": concurrency,
            "serial": bool(args.serial),
            "max_results": args.max_results,
            "search_depth": args.search_depth or DEFAULT_SEARCH_DEPTH,
            "topic": args.topic,
            "time_range": args.time_range,
            "start_date": args.start_date,
            "end_date": args.end_date,
            "include_domains": args.include_domains,
            "exclude_domains": args.exclude_domains,
            "include_images": args.include_images,
            "raw_content": args.raw_content,
            "country": args.country,
            "timeout": args.timeout,
        },
        "summary": {
            "query_count": len(queries),
            "success_count": success_count,
            "failed_count": failed_count,
            "credits_used": credits_total if has_credits else None,
        },
        "results": entries,
    }

    if success_count == 0:
        code, failure = first_error or (EXIT_REJECTED, {"http_status": None, "error": "all queries failed"})
        return code, batch_status, failure
    return EXIT_OK, batch_status, None


def run_extract(
    args: argparse.Namespace, api_key: str
) -> tuple[int, dict[str, Any] | None, dict[str, Any] | None]:
    _print_credit_estimate("extract", _estimate_extract_credits(args))
    if args.extract_depth:
        print(
            f"Note: --extract-depth {args.extract_depth!r} is ignored (Firecrawl always fully renders pages).",
            file=sys.stderr,
        )
    if args.chunks_per_source is not None:
        print(
            "Note: --chunks-per-source is ignored (Firecrawl highlights have no chunk-count control).",
            file=sys.stderr,
        )
    if args.format == "text":
        print(
            "Note: --format text maps to markdown (Firecrawl has no text tier).",
            file=sys.stderr,
        )

    results: list[dict[str, Any]] = []
    failed_results: list[dict[str, Any]] = []
    total_credits = 0
    image_count = 0
    first_error: tuple[int, str] | None = None

    for url in args.urls:
        status, payload = _post_json("/scrape", _build_extract_request(args, url), api_key, args.timeout)
        code = _exit_code_for(status, payload)
        if code == EXIT_OK:
            item = _normalize_extract_result(args, url, payload or {})
            results.append(item)
            credits = _scrape_credits_used(payload or {})
            if credits is not None:
                total_credits += credits
            if args.include_images and isinstance(item.get("images"), list):
                image_count += len(item["images"])
            continue
        failed_results.append({"url": url, "error": _error_message(status, payload)})
        if first_error is None:
            first_error = (code, f"scrape failed for {url}: {_error_message(status, payload)}")

    payload_out = {
        "command": "extract",
        "input": {
            "urls": args.urls,
            "extract_depth": args.extract_depth or DEFAULT_EXTRACT_DEPTH,
            "format": args.format,
            "query": args.query,
            "chunks_per_source": args.chunks_per_source,
            "include_images": args.include_images,
            "include_favicon": args.include_favicon,
            "timeout": args.timeout,
        },
        "data": {
            "results": results,
            "failed_results": failed_results,
            "credits_used": total_credits if total_credits else None,
            "cost_breakdown": _build_cost_breakdown(
                0,
                _cost_documents(results, "markdown"),
                total_credits if total_credits else None,
            ),
            "result_count": len(results),
            "failed_count": len(failed_results),
            "image_count": image_count,
        },
    }

    if not results:
        code, message = first_error or (EXIT_NETWORK_SERVER, "all scrapes failed")
        return code, None, {"http_status": None, "error": message}

    if failed_results:
        _, message = first_error or (EXIT_OK, "")
        print(f"Warning: {message}", file=sys.stderr)
    return EXIT_OK, payload_out, None


def run_usage(
    args: argparse.Namespace, api_key: str
) -> tuple[int, dict[str, Any] | None, dict[str, Any] | None]:
    """Report account credit usage. Consumes zero Firecrawl credits.

    With `--history N` a second read-only call fetches billing-period consumption.
    The current period is already served by the primary endpoint, so only earlier
    periods are merged in; consumers get the full history in `data.periods`.
    """
    status, payload = _get_json(USAGE_PATH, api_key, args.timeout)
    code = _exit_code_for(status, payload)
    if code != EXIT_OK:
        return code, None, {"http_status": status, "error": _error_message(status, payload)}
    response = payload or {}

    historical: dict[str, Any] | None = None
    if args.history:
        raw_data = response.get("data")
        response_data = raw_data if isinstance(raw_data, dict) else {}
        current = response_data.get("billingPeriodStart")
        h_status, h_payload = _get_json(USAGE_HISTORICAL_PATH, api_key, args.timeout)
        h_code = _exit_code_for(h_status, h_payload)
        if h_code == EXIT_OK:
            periods = (h_payload or {}).get("periods") or []
            historical = {
                "periods": [
                    p for p in periods
                    if isinstance(p, dict) and p.get("startDate") != current
                ][: args.history]
            }
        else:
            # The primary payload is valid, so this is not a failure; the gap is
            # reported on stderr and kept in `raw` so consumers can tell.
            print(
                f"Warning: historical periods unavailable ({_error_message(h_status, h_payload)}); "
                "reporting current billing period only.",
                file=sys.stderr,
            )
            historical = {"periods": []}

    return EXIT_OK, _normalize_usage_response(args, response, historical), None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)
    load_workspace_env(args.env_file)

    try:
        api_key = _get_api_key()
    except ApiError as exc:
        print(
            json.dumps({"command": args.command, "error": str(exc), "http_status": None}, ensure_ascii=False),
            file=sys.stderr,
        )
        return EXIT_AUTH

    try:
        if args.command == "search":
            code, payload, error = run_search(args, api_key)
        elif args.command == "extract":
            code, payload, error = run_extract(args, api_key)
        elif args.command == "usage":
            code, payload, error = run_usage(args, api_key)
        else:
            parser.error(f"Unsupported command: {args.command}")
            return EXIT_USAGE
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130

    if args.command == "search" and _is_batch(args):
        # Batch mode always prints the status envelope; on total failure the mapped
        # error also goes to stderr and becomes the exit code.
        _emit_payload(payload, None)
        if code != EXIT_OK:
            _report_failure("search", code, error or {"http_status": None, "error": "all queries failed"})
        return code

    if code != EXIT_OK:
        _report_failure(args.command, code, error or {"http_status": None, "error": "request failed"})
        return code

    _emit_payload(payload, _resolve_output_path(args))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

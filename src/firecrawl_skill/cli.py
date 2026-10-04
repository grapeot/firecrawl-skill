#!/usr/bin/env python3
"""firecrawl_skill CLI.

Agent-facing web search and URL extraction over the Firecrawl v2 API.
The command surface and output contract mirror tavily-skill; see docs/rfc.md.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

API_BASE = "https://api.firecrawl.dev/v2"
CREDIT_USAGE_URL = "https://api.firecrawl.dev/v2/team/credit-usage"

DEFAULT_MAX_RESULTS = 6
DEFAULT_TIMEOUT = 60
DEFAULT_RAW_CONTENT = "markdown"
DEFAULT_TOPIC = "general"
DEFAULT_SEARCH_DEPTH = "advanced"
DEFAULT_EXTRACT_DEPTH = "advanced"
DEFAULT_EXTRACT_FORMAT = "markdown"
MAX_RESULTS_LIMIT = 20
MAX_URLS_LIMIT = 20

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
    search_parser.add_argument("query", help="Search query")
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

    usage_parser = subparsers.add_parser("usage", help="Show Firecrawl account credit usage")
    usage_parser.set_defaults(stdout=False)
    usage_parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print the full JSON payload to stdout instead of writing it to the default output file",
    )
    usage_parser.add_argument(
        "--output",
        help="Write the full usage payload to a file and return status JSON on stdout",
    )
    usage_parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Request timeout in seconds (default: {DEFAULT_TIMEOUT})",
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
            payload = json.loads(exc.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = None
        if not isinstance(payload, dict):
            payload = None
        return status, payload
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError):
        return None, None

    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return status, None
    if not isinstance(payload, dict):
        return status, None
    return status, payload


def _get_json(url: str, api_key: str, timeout: float) -> tuple[int | None, dict[str, Any] | None]:
    """GET JSON from the Firecrawl API. Returns (http_status, parsed_body)."""
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = None
        if not isinstance(payload, dict):
            payload = None
        return status, payload
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError):
        return None, None

    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return status, None
    if not isinstance(payload, dict):
        return status, None
    return status, payload


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
            "job_id": response.get("id"),
            "result_count": len(results),
            "image_count": len(images),
        },
    }


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
# Output
# ---------------------------------------------------------------------------


def _emit_payload(payload: dict[str, Any], output_path: str | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if not output_path:
        print(text)
        return

    target = Path(output_path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")

    command = payload.get("command")
    data = payload.get("data", {})
    status_payload = {
        "command": command,
        "status": "ok",
        "output_mode": "file",
        "output_path": str(target),
        "payload_bytes": len(text.encode("utf-8")),
        "summary": {
            "result_count": data.get("result_count"),
            "failed_count": data.get("failed_count"),
            "image_count": data.get("image_count"),
            "credits_used": data.get("credits_used"),
        },
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
    elif args.command == "extract":
        seed = args.urls[0]
    else:
        seed = args.command
    slug = _slugify(seed)
    return str(get_default_output_dir() / f"{args.command}_{timestamp}_{slug}.json")


def _resolve_output_path(args: argparse.Namespace) -> str | None:
    if getattr(args, "stdout", False):
        return None
    if getattr(args, "output", None):
        return args.output
    return _default_output_path(args)


def _payload_schema(command: object) -> dict[str, Any]:
    base = {
        "command": "string",
        "input": "object",
    }
    if command == "usage":
        return {
            **base,
            "data": {
                "remaining_credits": "number|null",
                "plan_credits": "number|null",
                "billing_period_start": "string|null",
                "billing_period_end": "string|null",
            },
        }
    if command == "extract":
        return {
            **base,
            "data": {
                "results": "array",
                "failed_results": "array",
                "credits_used": "number|null",
                "result_count": "number",
                "failed_count": "number",
                "image_count": "number",
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
    print(f"Estimated Firecrawl credits: {estimate}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def run_search(
    args: argparse.Namespace, api_key: str
) -> tuple[int, dict[str, Any] | None, dict[str, Any] | None]:
    _print_credit_estimate("search", _estimate_search_credits(args))
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
    request = _build_search_request(args)
    status, payload = _post_json("/search", request, api_key, args.timeout)
    code = _exit_code_for(status, payload)
    if code != EXIT_OK:
        return code, None, {"http_status": status, "error": _error_message(status, payload)}
    return EXIT_OK, _normalize_search_response(args, payload or {}), None


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
    status, payload = _get_json(CREDIT_USAGE_URL, api_key, args.timeout)
    code = _exit_code_for(status, payload)
    if code != EXIT_OK:
        return code, None, {"http_status": status, "error": _error_message(status, payload)}

    raw_data = (payload or {}).get("data")
    data = raw_data if isinstance(raw_data, dict) else {}
    return EXIT_OK, {
        "command": "usage",
        "input": {"timeout": args.timeout},
        "data": {
            "remaining_credits": data.get("remainingCredits"),
            "plan_credits": data.get("planCredits"),
            "billing_period_start": data.get("billingPeriodStart"),
            "billing_period_end": data.get("billingPeriodEnd"),
        },
    }, None


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

    if code != EXIT_OK:
        _report_failure(args.command, code, error or {"http_status": None, "error": "request failed"})
        return code

    _emit_payload(payload, _resolve_output_path(args))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

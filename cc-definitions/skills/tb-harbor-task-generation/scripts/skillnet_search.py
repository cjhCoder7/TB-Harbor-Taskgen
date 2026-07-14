#!/usr/bin/env python3
"""Run one resilient SkillNet search and persist machine-readable evidence."""

from __future__ import annotations

import argparse
import json
import math
import os
import socket
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


DEFAULT_API_URL = "http://api-skillnet.openkg.cn/v1/search"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_RETRIES = 2
DEFAULT_BACKOFF_SECONDS = 1.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_LIMIT = 100
MAX_RETRIES = 4
MAX_TIMEOUT_SECONDS = 30.0
MAX_BACKOFF_SECONDS = 30.0
RETRYABLE_HTTP_STATUSES = {408, 425, 429}


class SearchProtocolError(RuntimeError):
    """The service returned a response that does not match its public schema."""


class SearchServiceError(RuntimeError):
    """The service returned a valid response with ``success: false``."""


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


RequestFn = Callable[[str, float], HTTPResponse]
SleepFn = Callable[[float], None]


def _limit(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= MAX_LIMIT:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_LIMIT}")
    return parsed


def _retries(value: str) -> int:
    parsed = int(value)
    if not 0 <= parsed <= MAX_RETRIES:
        raise argparse.ArgumentTypeError(f"must be between 0 and {MAX_RETRIES}")
    return parsed


def _bounded_positive_float(value: str, *, maximum: float) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or not 0 < parsed <= maximum:
        raise argparse.ArgumentTypeError(f"must be finite and between 0 and {maximum:g}")
    return parsed


def _timeout(value: str) -> float:
    return _bounded_positive_float(value, maximum=MAX_TIMEOUT_SECONDS)


def _backoff(value: str) -> float:
    return _bounded_positive_float(value, maximum=MAX_BACKOFF_SECONDS)


def _threshold(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 1.0:
        raise argparse.ArgumentTypeError("must be finite and between 0.0 and 1.0")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Search SkillNet once, retry transient failures, and save a structured JSON record. "
            "Invoke this helper separately for each query."
        )
    )
    parser.add_argument("--query", required=True, help="One compact search query.")
    parser.add_argument(
        "--mode",
        choices=("keyword", "vector"),
        default="keyword",
        help="SkillNet search mode (default: keyword).",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="JSON path below output/skillnet/ in the current workspace.",
    )
    parser.add_argument("--limit", type=_limit, default=10)
    parser.add_argument(
        "--threshold",
        type=_threshold,
        default=0.65,
        help="Vector similarity threshold (default: 0.65).",
    )
    parser.add_argument("--timeout", type=_timeout, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--retries",
        type=_retries,
        default=DEFAULT_RETRIES,
        help="Retries after the initial request (default: 2).",
    )
    parser.add_argument(
        "--backoff",
        type=_backoff,
        default=DEFAULT_BACKOFF_SECONDS,
        help="Initial exponential-backoff delay in seconds (default: 1).",
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("SKILLNET_SEARCH_URL", DEFAULT_API_URL),
        help=argparse.SUPPRESS,
    )
    return parser


def validate_query(query: str) -> str:
    normalized = query.strip()
    if not normalized:
        raise ValueError("query must not be empty")
    if len(normalized) > 300:
        raise ValueError("query must be at most 300 characters")
    if any(ord(character) < 32 for character in normalized):
        raise ValueError("query must not contain control characters")
    return normalized


def validate_api_url(api_url: str) -> str:
    parsed = urlsplit(api_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("api URL must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("api URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("api URL must not contain a query or fragment")
    return api_url.rstrip("/")


def resolve_output_path(raw_path: str, cwd: Path) -> Path:
    relative = Path(raw_path)
    if relative.is_absolute():
        raise ValueError("output path must be relative to the current workspace")
    if relative.suffix.lower() != ".json":
        raise ValueError("output path must end in .json")

    workspace = cwd.resolve()
    allowed_root = (workspace / "output" / "skillnet").resolve(strict=False)
    try:
        allowed_root.relative_to(workspace)
    except ValueError as exc:
        raise ValueError("output/skillnet must stay inside the current workspace") from exc

    resolved = (workspace / relative).resolve(strict=False)
    try:
        resolved.relative_to(allowed_root)
    except ValueError as exc:
        raise ValueError("output path must be below output/skillnet/") from exc
    return resolved


def build_request_url(
    api_url: str,
    *,
    query: str,
    mode: str,
    limit: int,
    threshold: float,
) -> str:
    parameters: dict[str, str | int | float] = {
        "q": query,
        "mode": mode,
        "limit": limit,
    }
    if mode == "keyword":
        parameters.update({"page": 1, "min_stars": 0, "sort_by": "stars"})
    else:
        parameters["threshold"] = threshold
    return f"{api_url}?{urlencode(parameters)}"


def default_request(url: str, timeout: float) -> HTTPResponse:
    request = Request(url, headers={"User-Agent": "tb-harbor-taskgen-skillnet-helper/1"})
    with urlopen(request, timeout=timeout) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise SearchProtocolError("response exceeds the 2 MiB safety limit")
        return HTTPResponse(
            status=int(response.status),
            headers=dict(response.headers.items()),
            body=body,
        )


def parse_response(body: bytes) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SearchProtocolError("response is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise SearchProtocolError("response root is not an object")
    if payload.get("success") is not True:
        raise SearchServiceError("SkillNet reported success=false")
    data = payload.get("data")
    meta = payload.get("meta")
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise SearchProtocolError("response data is not a list of objects")
    if not isinstance(meta, dict):
        raise SearchProtocolError("response meta is not an object")
    return data, meta


def _retryable_http_status(status: int) -> bool:
    return status in RETRYABLE_HTTP_STATUSES or 500 <= status <= 599


def _safe_retry_after(headers: Mapping[str, str] | None) -> float | None:
    if not headers:
        return None
    value = next((item for key, item in headers.items() if key.lower() == "retry-after"), None)
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, 30.0)


def _attempt_record(
    number: int,
    outcome: str,
    *,
    duration_ms: int,
    http_status: int | None,
    retryable: bool,
    error_type: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    return {
        "number": number,
        "outcome": outcome,
        "http_status": http_status,
        "retryable": retryable,
        "error_type": error_type,
        "message": message,
        "duration_ms": duration_ms,
    }


def search_with_retry(
    *,
    query: str,
    mode: str,
    limit: int,
    threshold: float,
    timeout: float,
    retries: int,
    backoff: float,
    api_url: str,
    request_fn: RequestFn = default_request,
    sleep_fn: SleepFn = time.sleep,
) -> dict[str, Any]:
    request_url = build_request_url(
        api_url,
        query=query,
        mode=mode,
        limit=limit,
        threshold=threshold,
    )
    attempts: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    status = "failed"
    total_attempts = retries + 1

    for attempt_number in range(1, total_attempts + 1):
        started = time.monotonic()
        retry_after: float | None = None
        try:
            response = request_fn(request_url, timeout)
            if not 200 <= response.status <= 299:
                raise HTTPError(
                    request_url,
                    response.status,
                    "unexpected HTTP status",
                    response.headers,
                    None,
                )
            results, meta = parse_response(response.body)
            status = "succeeded" if results else "no_results"
            attempts.append(
                _attempt_record(
                    attempt_number,
                    status,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    http_status=response.status,
                    retryable=False,
                )
            )
            break
        except HTTPError as exc:
            retryable = _retryable_http_status(exc.code)
            retry_after = _safe_retry_after(exc.headers)
            attempts.append(
                _attempt_record(
                    attempt_number,
                    "http_error",
                    duration_ms=round((time.monotonic() - started) * 1000),
                    http_status=exc.code,
                    retryable=retryable,
                    error_type="http_error",
                    message=f"SkillNet search returned HTTP {exc.code}",
                )
            )
        except (URLError, TimeoutError, socket.timeout, ConnectionError, OSError):
            retryable = True
            attempts.append(
                _attempt_record(
                    attempt_number,
                    "network_error",
                    duration_ms=round((time.monotonic() - started) * 1000),
                    http_status=None,
                    retryable=True,
                    error_type="network_error",
                    message="SkillNet search request failed",
                )
            )
        except SearchServiceError:
            retryable = True
            attempts.append(
                _attempt_record(
                    attempt_number,
                    "service_error",
                    duration_ms=round((time.monotonic() - started) * 1000),
                    http_status=200,
                    retryable=True,
                    error_type="service_error",
                    message="SkillNet search service reported failure",
                )
            )
        except (SearchProtocolError, ValueError, TypeError):
            retryable = True
            attempts.append(
                _attempt_record(
                    attempt_number,
                    "protocol_error",
                    duration_ms=round((time.monotonic() - started) * 1000),
                    http_status=200,
                    retryable=True,
                    error_type="protocol_error",
                    message="SkillNet search returned an invalid response",
                )
            )

        if not retryable or attempt_number == total_attempts:
            break
        delay = retry_after if retry_after is not None else backoff * (2 ** (attempt_number - 1))
        delay = min(delay, MAX_BACKOFF_SECONDS)
        sleep_fn(delay)

    parameters: dict[str, Any] = {"limit": limit}
    if mode == "keyword":
        parameters.update({"page": 1, "min_stars": 0, "sort_by": "stars"})
    else:
        parameters["threshold"] = threshold
    return {
        "schema_version": 1,
        "query": query,
        "mode": mode,
        "parameters": parameters,
        "status": status,
        "attempt_count": len(attempts),
        "attempts": attempts,
        "meta": meta,
        "results": results,
    }


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        query = validate_query(args.query)
        api_url = validate_api_url(args.api_url)
        output_path = resolve_output_path(args.output, Path.cwd())
    except ValueError as exc:
        parser.error(str(exc))

    payload = search_with_retry(
        query=query,
        mode=args.mode,
        limit=args.limit,
        threshold=args.threshold,
        timeout=args.timeout,
        retries=args.retries,
        backoff=args.backoff,
        api_url=api_url,
    )
    write_json_atomic(output_path, payload)
    summary = {
        "status": payload["status"],
        "result_count": len(payload["results"]),
        "attempt_count": payload["attempt_count"],
        "output": output_path.relative_to(Path.cwd().resolve()).as_posix(),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 1 if payload["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())

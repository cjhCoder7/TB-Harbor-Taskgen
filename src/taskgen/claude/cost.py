#!/usr/bin/env python3
"""Parse Claude Code stream-json logs into a compact cost summary."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "service_tier_input_tokens",
    "server_tool_use",
)

OPENROUTER_GENERATION_ENDPOINT = "https://openrouter.ai/api/v1/generation"
OPENROUTER_RETRY_STATUSES = {404, 408, 409, 425, 429, 500, 502, 503, 504, 524, 529}
OPENROUTER_TOTAL_FIELDS = (
    "tokens_prompt",
    "tokens_completion",
    "native_tokens_prompt",
    "native_tokens_completion",
    "native_tokens_cached",
    "native_tokens_reasoning",
    "native_tokens_completion_images",
    "num_media_prompt",
    "num_media_completion",
    "num_search_results",
    "total_cost",
    "usage",
    "upstream_inference_cost",
)
OPENROUTER_COMPACT_FIELDS = (
    "id",
    "created_at",
    "model",
    "provider_name",
    "router",
    "streamed",
    "cancelled",
    "finish_reason",
    "native_finish_reason",
    *OPENROUTER_TOTAL_FIELDS,
    "generation_time",
    "latency",
)


class OpenRouterQueryError(Exception):
    """OpenRouter generation metadata lookup failed."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def number_or_none(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    return None


def dict_or_none(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def read_json_events(path: Path) -> tuple[list[dict[str, Any]], int, int]:
    events: list[dict[str, Any]] = []
    line_count = 0
    invalid_json_line_count = 0

    if not path.exists():
        return events, line_count, invalid_json_line_count

    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line_count += 1
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            invalid_json_line_count += 1
            continue
        if isinstance(event, dict):
            events.append(event)

    return events, line_count, invalid_json_line_count


def ordered_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def event_generation_id(event: dict[str, Any]) -> str | None:
    for container in (
        event,
        dict_or_none(event.get("message")),
        dict_or_none(event.get("event")),
    ):
        if container is None:
            continue
        for key in ("id", "message_id", "generation_id"):
            value = string_or_none(container.get(key))
            if value and value.startswith("gen-"):
                return value
    return None


def find_openrouter_generation_ids(events: list[dict[str, Any]]) -> list[str]:
    generation_ids: list[str] = []
    for event in events:
        generation_id = event_generation_id(event)
        if generation_id:
            generation_ids.append(generation_id)
    return ordered_unique(generation_ids)


def find_message_usage(event: dict[str, Any]) -> tuple[str | None, str | None, dict[str, Any] | None]:
    message = dict_or_none(event.get("message")) or dict_or_none(event.get("event"))
    if message is None:
        return None, None, None

    usage = dict_or_none(message.get("usage"))
    if usage is None:
        return None, None, None

    message_id = message.get("id") or message.get("message_id") or event.get("message_id")
    model = message.get("model") or event.get("model")
    return (
        message_id if isinstance(message_id, str) else None,
        model if isinstance(model, str) else None,
        usage,
    )


def merge_token_totals(total: dict[str, int | float], usage: dict[str, Any]) -> None:
    for key in TOKEN_FIELDS:
        value = number_or_none(usage.get(key))
        if value is None:
            continue
        total[key] = total.get(key, 0) + value


def parse_claude_stream_log(stream_log: Path) -> dict[str, Any]:
    events, line_count, invalid_json_line_count = read_json_events(stream_log)
    openrouter_generation_ids = find_openrouter_generation_ids(events)

    init_event: dict[str, Any] | None = None
    result_event: dict[str, Any] | None = None
    assistant_usage_by_message: dict[str, dict[str, Any]] = {}
    anonymous_usage: list[dict[str, Any]] = []

    for event in events:
        if event.get("type") == "system" and event.get("subtype") == "init":
            init_event = event
        if event.get("type") == "result":
            result_event = event

        message_id, model, usage = find_message_usage(event)
        if usage is None:
            continue
        if message_id:
            assistant_usage_by_message[message_id] = {
                "message_id": message_id,
                "model": model,
                "usage": usage,
            }
        else:
            anonymous_usage.append({"model": model, "usage": usage})

    assistant_usage_totals: dict[str, int | float] = {}
    for item in assistant_usage_by_message.values():
        merge_token_totals(assistant_usage_totals, item["usage"])
    for item in anonymous_usage:
        merge_token_totals(assistant_usage_totals, item["usage"])

    result_usage = dict_or_none(result_event.get("usage")) if result_event else None
    provider_usage_cost = number_or_none(result_usage.get("cost")) if result_usage else None
    result_model_usage = None
    if result_event:
        result_model_usage = (
            dict_or_none(result_event.get("model_usage"))
            or dict_or_none(result_event.get("modelUsage"))
        )

    session_id = None
    model = None
    for candidate in (result_event, init_event):
        if not candidate:
            continue
        if session_id is None and isinstance(candidate.get("session_id"), str):
            session_id = candidate["session_id"]
        if model is None and isinstance(candidate.get("model"), str):
            model = candidate["model"]

    total_cost_usd = number_or_none(result_event.get("total_cost_usd")) if result_event else None

    summary = {
        "stream_log": str(stream_log),
        "parsed": stream_log.exists(),
        "line_count": line_count,
        "json_event_count": len(events),
        "invalid_json_line_count": invalid_json_line_count,
        "session_id": session_id,
        "model": model,
        "result_subtype": result_event.get("subtype") if result_event else None,
        "is_error": result_event.get("is_error") if result_event else None,
        "num_turns": result_event.get("num_turns") if result_event else None,
        "duration_ms": number_or_none(result_event.get("duration_ms")) if result_event else None,
        "duration_api_ms": number_or_none(result_event.get("duration_api_ms")) if result_event else None,
        "total_cost_usd": total_cost_usd,
        "provider_usage_cost": provider_usage_cost,
        "usage": result_usage,
        "model_usage": result_model_usage,
        "assistant_usage_totals": assistant_usage_totals or None,
        "assistant_message_count": len(assistant_usage_by_message) + len(anonymous_usage),
        "cost_source": "claude_stream_log" if total_cost_usd is not None else None,
        "claude_stream_total_cost_usd": total_cost_usd,
        "openrouter_generation_ids": openrouter_generation_ids,
        "openrouter_generation_count": len(openrouter_generation_ids),
    }
    return summary


def resolve_openrouter_api_key(env: dict[str, str] | None = None) -> str | None:
    resolved_env = os.environ if env is None else env
    direct_key = (resolved_env.get("OPENROUTER_API_KEY") or "").strip()
    if direct_key:
        return direct_key

    base_url = (
        resolved_env.get("OPENROUTER_BASE_URL")
        or resolved_env.get("ANTHROPIC_BASE_URL")
        or resolved_env.get("ANTHROPIC_API_URL")
        or resolved_env.get("CLAUDE_CODE_BASE_URL")
        or ""
    )
    if "openrouter.ai" not in base_url:
        return None
    fallback_key = (
        resolved_env.get("ANTHROPIC_AUTH_TOKEN")
        or resolved_env.get("ANTHROPIC_API_KEY")
        or ""
    ).strip()
    return fallback_key or None


def read_float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError:
        return default
    return min(max(value, minimum), maximum)


def read_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return min(max(value, minimum), maximum)


def openrouter_error_message(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text[:500]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message[:500]
    message = payload.get("message") if isinstance(payload, dict) else None
    if isinstance(message, str) and message:
        return message[:500]
    return text[:500]


def query_openrouter_generation(
    generation_id: str,
    api_key: str,
    *,
    endpoint: str = OPENROUTER_GENERATION_ENDPOINT,
    timeout: float = 10.0,
) -> dict[str, Any]:
    query_separator = "&" if "?" in endpoint else "?"
    url = f"{endpoint}{query_separator}{urlencode({'id': generation_id})}"
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            response_body = response.read()
    except HTTPError as error:
        message = openrouter_error_message(error.read())
        detail = f"OpenRouter generation query failed with HTTP {error.code}"
        if message:
            detail = f"{detail}: {message}"
        raise OpenRouterQueryError(detail, error.code) from error
    except URLError as error:
        raise OpenRouterQueryError(f"OpenRouter generation query failed: {error.reason}") from error
    except TimeoutError as error:
        raise OpenRouterQueryError("OpenRouter generation query timed out") from error

    try:
        payload = json.loads(response_body.decode("utf-8"))
    except json.JSONDecodeError as error:
        raise OpenRouterQueryError("OpenRouter generation query returned invalid JSON") from error

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise OpenRouterQueryError("OpenRouter generation query returned no data object")
    return data


def compact_openrouter_generation(data: dict[str, Any]) -> dict[str, Any]:
    return {field: data[field] for field in OPENROUTER_COMPACT_FIELDS if field in data}


def merge_numeric_totals(total: dict[str, int | float], data: dict[str, Any], fields: tuple[str, ...]) -> None:
    for field in fields:
        value = number_or_none(data.get(field))
        if value is None:
            continue
        total[field] = total.get(field, 0) + value


def increment_group(
    groups: dict[str, dict[str, Any]],
    key: str | None,
    data: dict[str, Any],
) -> None:
    if not key:
        return
    group = groups.setdefault(key, {"generation_count": 0})
    group["generation_count"] += 1
    merge_numeric_totals(group, data, OPENROUTER_TOTAL_FIELDS)


def fetch_openrouter_generation_stats(
    generation_ids: list[str],
    api_key: str,
    *,
    fetch_generation: Callable[[str, str], dict[str, Any]] | None = None,
    retry_count: int | None = None,
    retry_delay_seconds: float | None = None,
) -> dict[str, Any]:
    fetch = fetch_generation or query_openrouter_generation
    retries = retry_count if retry_count is not None else read_int_env("TASKGEN_OPENROUTER_RETRIES", 1, 0, 5)
    retry_delay = (
        retry_delay_seconds
        if retry_delay_seconds is not None
        else read_float_env("TASKGEN_OPENROUTER_RETRY_DELAY_SECONDS", 1.0, 0.0, 10.0)
    )

    generations: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    totals: dict[str, int | float] = {}
    by_model: dict[str, dict[str, Any]] = {}
    by_provider: dict[str, dict[str, Any]] = {}

    for generation_id in generation_ids:
        last_error: OpenRouterQueryError | None = None
        for attempt in range(retries + 1):
            try:
                data = fetch(generation_id, api_key)
                compact = compact_openrouter_generation(data)
                compact.setdefault("id", generation_id)
                generations.append(compact)
                merge_numeric_totals(totals, compact, OPENROUTER_TOTAL_FIELDS)
                increment_group(by_model, string_or_none(compact.get("model")), compact)
                increment_group(by_provider, string_or_none(compact.get("provider_name")), compact)
                last_error = None
                break
            except OpenRouterQueryError as error:
                last_error = error
                retryable = error.status in OPENROUTER_RETRY_STATUSES or error.status is None
                if not retryable or attempt >= retries:
                    break
                if retry_delay:
                    time.sleep(retry_delay)
        if last_error is not None:
            failure: dict[str, Any] = {
                "id": generation_id,
                "error": str(last_error),
            }
            if last_error.status is not None:
                failure["status"] = last_error.status
            failures.append(failure)

    return {
        "queried": True,
        "generation_count": len(generation_ids),
        "successful_generation_count": len(generations),
        "failed_generation_count": len(failures),
        "complete": len(failures) == 0 and len(generations) == len(generation_ids),
        "total_cost": totals.get("total_cost"),
        "usage": totals.get("usage"),
        "totals": totals,
        "by_model": by_model or None,
        "by_provider": by_provider or None,
        "generations": generations,
        "failures": failures,
    }


def enrich_with_openrouter_generation_stats(
    summary: dict[str, Any],
    *,
    api_key: str | None = None,
    fetch_generation: Callable[[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    generation_ids = summary.get("openrouter_generation_ids")
    if not isinstance(generation_ids, list) or not all(isinstance(item, str) for item in generation_ids):
        generation_ids = []

    if not generation_ids:
        summary["openrouter"] = {
            "queried": False,
            "reason": "no_generation_ids",
            "generation_count": 0,
        }
        return summary

    resolved_api_key = api_key if api_key is not None else resolve_openrouter_api_key()
    if not resolved_api_key:
        summary["openrouter"] = {
            "queried": False,
            "reason": "missing_api_key",
            "generation_count": len(generation_ids),
        }
        return summary

    openrouter_summary = fetch_openrouter_generation_stats(
        generation_ids,
        resolved_api_key,
        fetch_generation=fetch_generation,
    )
    summary["openrouter"] = openrouter_summary

    total_cost = number_or_none(openrouter_summary.get("total_cost"))
    if total_cost is not None:
        summary["openrouter_total_cost_usd"] = total_cost
    if openrouter_summary.get("complete") and total_cost is not None:
        summary["total_cost_usd"] = total_cost
        summary["cost_source"] = "openrouter_generation_api"
    return summary


def summarize_claude_stream_log(
    stream_log: Path,
    *,
    query_openrouter: bool = True,
    openrouter_api_key: str | None = None,
    fetch_openrouter_generation: Callable[[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    summary = parse_claude_stream_log(stream_log)
    if query_openrouter:
        enrich_with_openrouter_generation_stats(
            summary,
            api_key=openrouter_api_key,
            fetch_generation=fetch_openrouter_generation,
        )
    return summary


def format_cost_summary(summary: dict[str, Any]) -> str:
    parts: list[str] = []

    model = summary.get("model")
    if isinstance(model, str) and model:
        parts.append(f"model={model}")

    total_cost_usd = number_or_none(summary.get("total_cost_usd"))
    if total_cost_usd is not None:
        parts.append(f"cost=${total_cost_usd:.6f}")

    num_turns = number_or_none(summary.get("num_turns"))
    if num_turns is not None:
        parts.append(f"turns={int(num_turns)}")

    duration_ms = number_or_none(summary.get("duration_ms"))
    if duration_ms is not None:
        parts.append(f"duration={duration_ms / 1000:.1f}s")

    result_subtype = summary.get("result_subtype")
    if isinstance(result_subtype, str) and result_subtype:
        parts.append(f"result={result_subtype}")

    return ", ".join(parts) if parts else "cost unavailable"


def write_cost_summary(summary: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def command_summarize(args: argparse.Namespace) -> int:
    summary = summarize_claude_stream_log(
        args.stream_log,
        query_openrouter=not args.no_openrouter_query,
    )
    if args.output:
        write_cost_summary(summary, args.output)
    else:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    summarize = subparsers.add_parser("summarize", help="Parse one claude-code.txt stream-json log.")
    summarize.add_argument("stream_log", type=Path)
    summarize.add_argument("--output", type=Path)
    summarize.add_argument(
        "--no-openrouter-query",
        action="store_true",
        help="Do not query OpenRouter generation metadata even when credentials are configured.",
    )
    summarize.set_defaults(func=command_summarize)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

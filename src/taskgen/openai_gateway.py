#!/usr/bin/env python3
"""Lifecycle management for a local LiteLLM OpenAI-compatible gateway."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_STARTUP_TIMEOUT_SEC = 30.0
DEFAULT_SHUTDOWN_TIMEOUT_SEC = 10.0
LITELLM_EXECUTABLE = "litellm"
UPSTREAM_ENV_KEYS = ("OPENAI_BASE_URL", "OPENAI_API_KEY")
REMOVED_FROM_CLAUDE_ENV = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENROUTER_API_KEY",
    "OPENROUTER_BASE_URL",
)
MODEL_ENV_KEYS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)


class OpenAIGatewayError(RuntimeError):
    """Raised when the local OpenAI-compatible gateway cannot be used."""


@dataclass(frozen=True)
class OpenAIGateway:
    model: str
    base_url: str
    pid: int


def _require_upstream_environment() -> dict[str, str]:
    values: dict[str, str] = {}
    for key in UPSTREAM_ENV_KEYS:
        value = os.environ.get(key)
        if value is None or not value.strip():
            raise OpenAIGatewayError(
                f"{key} is not set; copy scripts/env_openai_init.example.sh to "
                "scripts/env_openai_init.sh and fill in the local value"
            )
        values[key] = value.strip()
    return values


def _resolve_litellm_executable() -> str:
    executable = shutil.which(LITELLM_EXECUTABLE)
    if executable is None:
        raise OpenAIGatewayError(
            "LiteLLM executable was not found on PATH; run scripts/tool_init.sh first"
        )
    return executable


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _write_private_text(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


def _gateway_config(model: str) -> dict[str, Any]:
    return {
        "model_list": [
            {
                "model_name": model,
                "litellm_params": {
                    "model": f"openai/{model}",
                    "api_base": "os.environ/OPENAI_BASE_URL",
                    "api_key": "os.environ/OPENAI_API_KEY",
                    # Claude Code's metadata.user_id is optional attribution.
                    # LiteLLM maps it to `user`, which some otherwise compatible
                    # Responses endpoints reject.
                    "additional_drop_params": ["user"],
                },
            }
        ],
        "general_settings": {
            "master_key": "os.environ/LITELLM_MASTER_KEY",
        },
    }


def _request_json(url: str, *, api_key: str | None, timeout_sec: float) -> Any:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key is not None else {}
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout_sec) as response:
        if response.getcode() != 200:
            raise OSError(f"unexpected HTTP status {response.getcode()}")
        body = response.read()
    return json.loads(body)


def _registered_model(payload: Any, model: str) -> bool:
    if not isinstance(payload, dict):
        return False
    entries = payload.get("data")
    if not isinstance(entries, list):
        return False
    return any(isinstance(entry, dict) and entry.get("id") == model for entry in entries)


def _wait_until_ready(
    process: subprocess.Popen[bytes],
    base_url: str,
    master_key: str,
    model: str,
    timeout_sec: float,
) -> None:
    deadline = time.monotonic() + timeout_sec
    health_ready = False
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            raise OpenAIGatewayError(
                f"LiteLLM exited during startup with status {exit_code}; "
                "check the OpenAI-compatible URL and LiteLLM installation"
            )

        request_timeout = max(0.05, min(0.5, deadline - time.monotonic()))
        try:
            if not health_ready:
                health_payload = _request_json(
                    f"{base_url}/health/readiness",
                    api_key=None,
                    timeout_sec=request_timeout,
                )
                health_ready = (
                    isinstance(health_payload, dict)
                    and health_payload.get("status") == "healthy"
                )
            if health_ready:
                models_payload = _request_json(
                    f"{base_url}/v1/models",
                    api_key=master_key,
                    timeout_sec=request_timeout,
                )
                if _registered_model(models_payload, model):
                    return
        except (HTTPError, URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError):
            pass
        time.sleep(0.1)

    raise OpenAIGatewayError(
        f"LiteLLM did not become ready within {timeout_sec:g} seconds "
        f"with model {model!r} registered"
    )


def _terminate_process_group(process: subprocess.Popen[bytes], timeout_sec: float) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        process.poll()
        time.sleep(0.05)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    try:
        process.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        pass


def _exit_for_signal(signum: int, _frame: FrameType | None) -> None:
    raise SystemExit(128 + signum)


@contextmanager
def _cleanup_signal_handlers() -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    previous: dict[int, Any] = {}
    for signal_name in ("SIGTERM", "SIGHUP"):
        signum = getattr(signal, signal_name, None)
        if signum is None:
            continue
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, _exit_for_signal)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


@contextmanager
def _temporary_environment(
    updates: Mapping[str, str],
    removals: tuple[str, ...],
) -> Iterator[None]:
    affected = set(updates) | set(removals)
    original = {key: os.environ.get(key) for key in affected}
    try:
        for key in removals:
            os.environ.pop(key, None)
        os.environ.update(updates)
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def openai_gateway(
    model: str,
    *,
    startup_timeout_sec: float = DEFAULT_STARTUP_TIMEOUT_SEC,
    shutdown_timeout_sec: float = DEFAULT_SHUTDOWN_TIMEOUT_SEC,
) -> Iterator[OpenAIGateway]:
    """Start one loopback LiteLLM proxy and expose it to child Claude runs."""

    if not model or not model.strip():
        raise OpenAIGatewayError("the OpenAI-compatible model name must not be empty")
    model = model.strip()
    upstream_environment = _require_upstream_environment()
    executable = _resolve_litellm_executable()
    master_key = f"sk-taskgen-{secrets.token_urlsafe(32)}"
    port = _reserve_loopback_port()
    base_url = f"http://127.0.0.1:{port}"

    with tempfile.TemporaryDirectory(prefix="taskgen-litellm-") as temporary_directory:
        temp_root = Path(temporary_directory)
        config_path = temp_root / "config.json"
        log_path = temp_root / "proxy.log"
        _write_private_text(config_path, json.dumps(_gateway_config(model), indent=2))

        log_descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(log_descriptor, "wb", buffering=0) as log_handle:
            proxy_environment = os.environ.copy()
            proxy_environment.update(upstream_environment)
            # Use LiteLLM's normal Anthropic Messages routing. LiteLLM's
            # default OpenAI-provider path uses the Responses API; remove any
            # inherited opt-out that would force it onto Chat Completions.
            proxy_environment.pop(
                "LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES",
                None,
            )
            proxy_environment.update(
                {
                    "LITELLM_MASTER_KEY": master_key,
                    "LITELLM_TELEMETRY": "False",
                }
            )
            command = [
                executable,
                "--config",
                str(config_path),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--num_workers",
                "1",
            ]
            process: subprocess.Popen[bytes] | None = None
            ready = False
            with _cleanup_signal_handlers():
                try:
                    process = subprocess.Popen(
                        command,
                        cwd=temp_root,
                        env=proxy_environment,
                        stdin=subprocess.DEVNULL,
                        stdout=log_handle,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    _wait_until_ready(
                        process,
                        base_url,
                        master_key,
                        model,
                        startup_timeout_sec,
                    )
                    ready = True

                    claude_environment = {
                        "ANTHROPIC_BASE_URL": base_url,
                        "ANTHROPIC_AUTH_TOKEN": master_key,
                        "TASKGEN_OPENAI_GATEWAY_ACTIVE": "1",
                        **{key: model for key in MODEL_ENV_KEYS},
                    }
                    with _temporary_environment(
                        claude_environment,
                        REMOVED_FROM_CLAUDE_ENV,
                    ):
                        print(
                            "openai gateway: local LiteLLM ready for model "
                            f"{model!r} on {base_url}; upstream is checked on first request"
                        )
                        yield OpenAIGateway(model=model, base_url=base_url, pid=process.pid)
                finally:
                    if process is not None:
                        return_code_before_cleanup = process.poll()
                        _terminate_process_group(process, shutdown_timeout_sec)
                        if ready and return_code_before_cleanup is not None:
                            print(
                                "openai gateway: local LiteLLM exited unexpectedly for model "
                                f"{model!r} with exit_code={return_code_before_cleanup}"
                            )
                        elif ready:
                            print(f"openai gateway: local LiteLLM stopped for model {model!r}")
                        else:
                            print(
                                "openai gateway: local LiteLLM startup failed for model "
                                f"{model!r}; process cleaned up"
                            )

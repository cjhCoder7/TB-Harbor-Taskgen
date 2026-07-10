#!/usr/bin/env python3
"""Configuration loading for TB Harbor task generation scripts."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
MODEL_CONFIG_FILENAME = "model.json"
DEFAULT_CLAUDE_CODE_TIMEOUT_SEC = 1800.0
PHASE_EFFORT_ALIASES = {
    "phase1": ("phase1", "brainstorm", "seed-brainstorm"),
    "phase2": ("phase2", "skillnet", "skillnet-research"),
    "phase3": ("phase3", "generate", "task-generation"),
    "phase4": ("phase4", "check", "oracle-nop"),
    "phase5": ("phase5", "review", "task-review"),
    "phase6": ("phase6", "repair", "task-repair"),
    "phase7": ("phase7", "finalize", "archive", "organize", "task-finalize"),
}
PHASE_EFFORT_KEYS = {
    key
    for aliases in PHASE_EFFORT_ALIASES.values()
    for key in aliases
}


@dataclass(frozen=True)
class ModelConfig:
    default_model: str | None = None
    default_effort: str | None = None
    phase_efforts: dict[str, str] = field(default_factory=dict)
    claude_code_path: str | None = None
    claude_code_timeout_sec: float = DEFAULT_CLAUDE_CODE_TIMEOUT_SEC


def load_model_config(root: Path) -> ModelConfig:
    config_path = root / MODEL_CONFIG_FILENAME
    if not config_path.exists():
        return ModelConfig()

    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {config_path}: {exc}") from None

    if not isinstance(payload, dict):
        raise SystemExit(f"{MODEL_CONFIG_FILENAME} must contain a JSON object")

    default_model = read_optional_non_empty_string(payload, "default_model", MODEL_CONFIG_FILENAME)
    default_effort = read_optional_non_empty_string(payload, "default_effort", MODEL_CONFIG_FILENAME)
    phase_efforts = read_phase_efforts(payload)
    claude_code_path = read_optional_non_empty_string(payload, "claude_code_path", MODEL_CONFIG_FILENAME)
    claude_code_timeout_sec = read_positive_number(
        payload,
        "claude_code_timeout_sec",
        MODEL_CONFIG_FILENAME,
        default=DEFAULT_CLAUDE_CODE_TIMEOUT_SEC,
    )
    if default_effort is not None and default_effort not in EFFORT_LEVELS:
        allowed = ", ".join(EFFORT_LEVELS)
        raise SystemExit(f"{MODEL_CONFIG_FILENAME}.default_effort must be one of: {allowed}")

    return ModelConfig(
        default_model=default_model,
        default_effort=default_effort,
        phase_efforts=phase_efforts,
        claude_code_path=claude_code_path,
        claude_code_timeout_sec=claude_code_timeout_sec,
    )


def read_optional_non_empty_string(payload: dict[str, Any], key: str, source: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"{source}.{key} must be a non-empty string when set")
    return value.strip()


def read_positive_number(
    payload: dict[str, Any],
    key: str,
    source: str,
    *,
    default: float,
) -> float:
    value = payload.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise SystemExit(f"{source}.{key} must be a positive finite number")
    try:
        numeric_value = float(value)
    except OverflowError:
        raise SystemExit(f"{source}.{key} must be a positive finite number") from None
    if not math.isfinite(numeric_value) or numeric_value <= 0:
        raise SystemExit(f"{source}.{key} must be a positive finite number")
    return numeric_value


def read_phase_efforts(payload: dict[str, Any]) -> dict[str, str]:
    value = payload.get("phase_efforts")
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SystemExit(f"{MODEL_CONFIG_FILENAME}.phase_efforts must be an object when set")

    phase_efforts: dict[str, str] = {}
    allowed_keys = ", ".join(sorted(PHASE_EFFORT_KEYS))
    allowed_efforts = ", ".join(EFFORT_LEVELS)
    for raw_key, raw_effort in value.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise SystemExit(f"{MODEL_CONFIG_FILENAME}.phase_efforts keys must be non-empty strings")
        key = raw_key.strip()
        if key not in PHASE_EFFORT_KEYS:
            raise SystemExit(
                f"{MODEL_CONFIG_FILENAME}.phase_efforts has unknown phase key {key!r}; "
                f"expected one of: {allowed_keys}"
            )
        if not isinstance(raw_effort, str) or not raw_effort.strip():
            raise SystemExit(f"{MODEL_CONFIG_FILENAME}.phase_efforts.{key} must be a non-empty string")
        effort = raw_effort.strip()
        if effort not in EFFORT_LEVELS:
            raise SystemExit(
                f"{MODEL_CONFIG_FILENAME}.phase_efforts.{key} must be one of: {allowed_efforts}"
            )
        phase_efforts[key] = effort
    return phase_efforts


def phase_effort_lookup_keys(phase: str | None) -> list[str]:
    if phase is None:
        return []
    phase = phase.strip()
    if not phase:
        return []

    keys = [phase]
    for canonical, aliases in PHASE_EFFORT_ALIASES.items():
        if phase in aliases:
            keys.extend([canonical, *aliases])
            break

    seen: set[str] = set()
    unique_keys: list[str] = []
    for key in keys:
        if key not in seen:
            unique_keys.append(key)
            seen.add(key)
    return unique_keys


def resolve_model_name(root: Path, explicit_model: str | None) -> str | None:
    if explicit_model:
        return explicit_model
    return load_model_config(root).default_model


def resolve_effort_level(root: Path, explicit_effort: str | None, phase: str | None = None) -> str | None:
    if explicit_effort:
        return explicit_effort
    config = load_model_config(root)
    for key in phase_effort_lookup_keys(phase):
        effort = config.phase_efforts.get(key)
        if effort is not None:
            return effort
    return config.default_effort


def resolve_claude_code_path(root: Path) -> Path | None:
    configured_path = load_model_config(root).claude_code_path
    if configured_path is None:
        return None

    path = Path(configured_path)
    if path.is_absolute():
        return path
    return root / path


def resolve_claude_code_timeout_sec(root: Path) -> float:
    return load_model_config(root).claude_code_timeout_sec

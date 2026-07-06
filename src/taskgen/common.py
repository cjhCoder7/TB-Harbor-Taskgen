#!/usr/bin/env python3
"""Shared utilities for TB Harbor task generation scripts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SAFE_PATH_SEGMENT_RE = re.compile(r"[A-Za-z0-9._-]+")


@dataclass
class ValidationReport:
    phase: str
    seed_id: str
    checked_paths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.errors


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def validate_path_segment(value: str, label: str) -> list[str]:
    if not value:
        return [f"{label} must be non-empty"]
    if value in {".", ".."}:
        return [f"{label} must be a normal path segment, got {value!r}"]
    if not SAFE_PATH_SEGMENT_RE.fullmatch(value):
        return [
            f"{label} must be a single path-safe segment matching "
            "[A-Za-z0-9._-]+"
        ]
    return []


def render_template(template: str, seed_id: str, idea_id: str, task_id: str) -> str:
    return template.format(seed_id=seed_id, idea_id=idea_id, task_id=task_id)


def resolve_display_path(root: Path, rendered: str) -> Path:
    if rendered.startswith("../"):
        return (root / rendered).resolve()
    return root / rendered


def load_json(path: Path, report: ValidationReport) -> Any | None:
    report.checked_paths.append(str(path))
    if not path.exists():
        report.errors.append(f"missing JSON file: {path}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        report.errors.append(f"invalid JSON in {path}: {exc}")
        return None


def require_object(value: Any, path: str, report: ValidationReport) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        report.errors.append(f"{path} must be an object")
        return None
    return value


def require_string(obj: dict[str, Any], key: str, path: str, report: ValidationReport) -> str | None:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        report.errors.append(f"{path}.{key} must be a non-empty string")
        return None
    return value


def require_string_list(
    obj: dict[str, Any],
    key: str,
    path: str,
    report: ValidationReport,
    *,
    min_items: int = 0,
) -> list[str] | None:
    value = obj.get(key)
    if not isinstance(value, list):
        report.errors.append(f"{path}.{key} must be a list")
        return None
    if len(value) < min_items:
        report.errors.append(f"{path}.{key} must contain at least {min_items} item(s)")
        return None
    bad_items = [
        index
        for index, item in enumerate(value)
        if not isinstance(item, str) or not item.strip()
    ]
    if bad_items:
        report.errors.append(f"{path}.{key} contains non-string or empty items at indexes {bad_items}")
        return None
    return value


def print_report(report: ValidationReport, as_json: bool) -> int:
    payload = {
        "phase": report.phase,
        "seed_id": report.seed_id,
        "passed": report.passed,
        "checked_paths": report.checked_paths,
        "errors": report.errors,
        "warnings": report.warnings,
    }
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        status = "PASS" if report.passed else "FAIL"
        print(f"{status} {report.phase} validation for seed {report.seed_id}")
        if report.errors:
            print("\nErrors:")
            for error in report.errors:
                print(f"- {error}")
        if report.warnings:
            print("\nWarnings:")
            for warning in report.warnings:
                print(f"- {warning}")
        print("\nChecked paths:")
        for path in report.checked_paths:
            print(f"- {path}")
    return 0 if report.passed else 1

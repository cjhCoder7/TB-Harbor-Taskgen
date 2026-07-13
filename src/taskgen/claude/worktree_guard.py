#!/usr/bin/env python3
"""Prevent Claude Code from leaving the taskgen run workspace via worktrees."""

from __future__ import annotations

import json
import sys
from typing import Any


PRE_TOOL_USE = "PreToolUse"
WORKTREE_CREATE = "WorktreeCreate"


class GuardInputError(ValueError):
    """A hook event did not have the shape required for a safe decision."""


def _pre_tool_output(
    decision: str,
    reason: str,
    *,
    updated_input: dict[str, Any] | None = None,
    additional_context: str | None = None,
) -> dict[str, Any]:
    output: dict[str, Any] = {
        "hookEventName": PRE_TOOL_USE,
        "permissionDecision": decision,
        "permissionDecisionReason": reason,
    }
    if updated_input is not None:
        output["updatedInput"] = updated_input
    if additional_context is not None:
        output["additionalContext"] = additional_context
    return {"hookSpecificOutput": output}


def evaluate_pre_tool_use(event: dict[str, Any]) -> dict[str, Any] | None:
    """Return a Claude Code hook response, or ``None`` for an unrelated call."""

    tool_name = event.get("tool_name")
    if tool_name == "EnterWorktree":
        return _pre_tool_output(
            "deny",
            "Taskgen already provides an isolated run workspace; entering a Git worktree is disabled.",
        )

    if tool_name not in {"Agent", "Task"}:
        return None

    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        raise GuardInputError(f"{tool_name} tool_input must be a JSON object")
    if "isolation" not in tool_input:
        return None

    updated_input = dict(tool_input)
    updated_input.pop("isolation", None)
    return _pre_tool_output(
        "allow",
        "Taskgen removed subagent isolation so the subagent uses the existing run workspace.",
        updated_input=updated_input,
        additional_context=(
            "The Agent call is running in taskgen's existing isolated workspace. "
            "Keep using the current directory and do not create or enter a Git worktree."
        ),
    )


def main() -> int:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise GuardInputError("hook input must be a JSON object")

        event_name = event.get("hook_event_name")
        if not isinstance(event_name, str):
            raise GuardInputError("hook_event_name must be a string")
        if event_name == WORKTREE_CREATE:
            print(
                "Taskgen blocks Claude Code worktree creation; use the existing run workspace.",
                file=sys.stderr,
            )
            return 2
        if event_name != PRE_TOOL_USE:
            raise GuardInputError(f"unsupported hook event: {event_name}")
        if not isinstance(event.get("tool_name"), str):
            raise GuardInputError("PreToolUse tool_name must be a string")

        output = evaluate_pre_tool_use(event)
        if output is not None:
            json.dump(output, sys.stdout, ensure_ascii=False, separators=(",", ":"))
            sys.stdout.write("\n")
        return 0
    except (GuardInputError, json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        print(f"Taskgen worktree guard rejected malformed hook input: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

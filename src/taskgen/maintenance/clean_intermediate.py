#!/usr/bin/env python3
"""Clean generated intermediate artifacts while preserving run directory skeletons."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from taskgen.common import project_root


RUN_SKELETON_DIRS = (
    "prompts",
    "brainstorm",
    "skillnet",
    "oracle-nop-check",
    "reviews",
    "workspace",
    "claude-sessions/seed-brainstorm",
    "claude-sessions/skillnet-research",
    "claude-sessions/task-generation",
    "claude-sessions/task-review",
    "claude-sessions/task-repair",
)
GITKEEP_TEXT = "# Keep this runtime artifact directory in git.\n"


def clean_targets(root: Path) -> list[Path]:
    runs_root = root / "runs"
    return [
        runs_root / "prompts",
        runs_root / "brainstorm",
        runs_root / "skillnet",
        runs_root / "oracle-nop-check",
        runs_root / "reviews",
        runs_root / "workspace",
        runs_root / "claude-sessions",
        runs_root / "task-manifest.jsonl",
        *(root / "src").rglob("__pycache__"),
        *(root / "scripts").rglob("__pycache__"),
        *(root / "tests").rglob("__pycache__"),
    ]


def restore_skeleton(root: Path) -> None:
    runs_root = root / "runs"
    for rel_path in RUN_SKELETON_DIRS:
        skeleton_dir = runs_root / rel_path
        skeleton_dir.mkdir(parents=True, exist_ok=True)
        (skeleton_dir / ".gitkeep").write_text(GITKEEP_TEXT, encoding="utf-8")


def remove_target(target: Path) -> None:
    if not target.exists():
        return
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()


def command_clean(args: argparse.Namespace) -> int:
    root = project_root()
    targets = clean_targets(root)

    print("Clean targets:")
    for target in targets:
        if target.exists():
            print(f"  {target}")

    if not args.apply:
        print("\nDry-run only. Re-run with --apply to delete these artifacts and restore the runs skeleton.")
        return 0

    for target in targets:
        remove_target(target)
    restore_skeleton(root)
    print("Cleaned intermediate artifacts and restored runs directory skeleton.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clean-intermediate.sh", description=__doc__)
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return command_clean(args)


if __name__ == "__main__":
    sys.exit(main())

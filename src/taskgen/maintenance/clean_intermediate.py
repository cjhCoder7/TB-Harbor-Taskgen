#!/usr/bin/env python3
"""Clean generated intermediate artifacts while preserving run directory skeletons."""

from __future__ import annotations

import argparse
import fcntl
import os
import shutil
import sys
from pathlib import Path
from typing import TextIO

from taskgen.common import project_root


RUN_SKELETON_DIRS = (
    "prompts",
    "brainstorm",
    "skillnet",
    "oracle-nop-check",
    "reviews",
    "workspace",
    "finalization-transactions",
    "output-sync-transactions",
    "claude-sessions/seed-brainstorm",
    "claude-sessions/skillnet-research",
    "claude-sessions/task-generation",
    "claude-sessions/task-review",
    "claude-sessions/task-repair",
)
GITKEEP_TEXT = "# Keep this runtime artifact directory in git.\n"
ACTIVE_RUN_LOCK = ".active-runs.lock"
ACTIVE_RUN_MARKER = ".active"
TRANSACTION_DIRS = ("finalization-transactions", "output-sync-transactions")


def clean_targets(root: Path, *, drop_manifest: bool = False) -> list[Path]:
    runs_root = root / "runs"
    targets = [
        runs_root / "prompts",
        runs_root / "brainstorm",
        runs_root / "skillnet",
        runs_root / "oracle-nop-check",
        runs_root / "reviews",
        runs_root / "workspace",
        runs_root / "finalization-transactions",
        runs_root / "output-sync-transactions",
        runs_root / "claude-sessions",
        *(root / "src").rglob("__pycache__"),
        *(root / "scripts").rglob("__pycache__"),
        *(root / "tests").rglob("__pycache__"),
    ]
    if drop_manifest:
        targets.insert(9, runs_root / "task-manifest.jsonl")
    return targets


def active_run_markers(root: Path) -> list[Path]:
    sessions_root = root / "runs/claude-sessions"
    if not sessions_root.is_dir():
        return []
    return sorted(sessions_root.rglob(ACTIVE_RUN_MARKER))


def pending_transaction_files(root: Path) -> list[Path]:
    pending: list[Path] = []
    for directory_name in TRANSACTION_DIRS:
        directory = root / "runs" / directory_name
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if path.name != ".gitkeep":
                pending.append(path)
    return sorted(pending)


def acquire_cleanup_lock(root: Path) -> TextIO | None:
    lock_path = root / "runs" / ACTIVE_RUN_LOCK
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def restore_skeleton(root: Path) -> None:
    runs_root = root / "runs"
    for rel_path in RUN_SKELETON_DIRS:
        skeleton_dir = runs_root / rel_path
        skeleton_dir.mkdir(parents=True, exist_ok=True)
        (skeleton_dir / ".gitkeep").write_text(GITKEEP_TEXT, encoding="utf-8")


def remove_target(target: Path) -> None:
    if not os.path.lexists(target):
        return
    if target.is_symlink() or not target.is_dir():
        target.unlink()
    else:
        shutil.rmtree(target)


def command_clean(args: argparse.Namespace) -> int:
    root = project_root()
    targets = clean_targets(root, drop_manifest=args.drop_manifest)
    pending_transactions = pending_transaction_files(root)

    print("Clean targets:")
    for target in targets:
        if os.path.lexists(target):
            print(f"  {target}")

    if not args.apply:
        if not args.drop_manifest:
            print("\nPreserved by default: runs/task-manifest.jsonl")
        if pending_transactions:
            print("\nPending recovery transactions (cleanup will refuse without --discard-transactions):")
            for transaction in pending_transactions:
                print(f"  {transaction}")
        print("\nDry-run only. Re-run with --apply to delete these artifacts and restore the runs skeleton.")
        return 0

    cleanup_lock = acquire_cleanup_lock(root)
    markers = active_run_markers(root)
    if cleanup_lock is None or markers:
        if not args.force_active:
            print("refusing to clean while a pipeline run is active", file=sys.stderr)
            for marker in markers:
                print(f"- active marker: {marker}", file=sys.stderr)
            if cleanup_lock is not None:
                fcntl.flock(cleanup_lock.fileno(), fcntl.LOCK_UN)
                cleanup_lock.close()
            return 1
        print("warning: forcing cleanup while a pipeline run may be active", file=sys.stderr)

    pending_transactions = pending_transaction_files(root)
    if pending_transactions and not getattr(args, "discard_transactions", False):
        print(
            "refusing to discard pending output/finalization recovery transactions",
            file=sys.stderr,
        )
        for transaction in pending_transactions:
            print(f"- pending transaction: {transaction}", file=sys.stderr)
        if cleanup_lock is not None:
            fcntl.flock(cleanup_lock.fileno(), fcntl.LOCK_UN)
            cleanup_lock.close()
        return 1
    if pending_transactions:
        print("warning: explicitly discarding pending recovery transactions", file=sys.stderr)

    try:
        for target in targets:
            remove_target(target)
        restore_skeleton(root)
    finally:
        if cleanup_lock is not None:
            fcntl.flock(cleanup_lock.fileno(), fcntl.LOCK_UN)
            cleanup_lock.close()
    print("Cleaned intermediate artifacts and restored runs directory skeleton.")
    if not args.drop_manifest:
        print("Preserved runs/task-manifest.jsonl; use --drop-manifest to remove audit history.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clean-intermediate.sh", description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--drop-manifest",
        action="store_true",
        help="Also delete the append-only task manifest. It is preserved by default.",
    )
    parser.add_argument(
        "--force-active",
        action="store_true",
        help="Allow cleanup even when an active pipeline run is detected.",
    )
    parser.add_argument(
        "--discard-transactions",
        action="store_true",
        help="Discard pending crash-recovery journals instead of refusing cleanup.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return command_clean(args)


if __name__ == "__main__":
    sys.exit(main())

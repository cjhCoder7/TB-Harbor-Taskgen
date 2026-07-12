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
TRUSTED_CONTAINER_DIRS = ("runs", "src", "scripts", "tests")


class CleanupSafetyError(RuntimeError):
    """Raised when cleanup cannot prove that a path stays inside the project."""


def validate_cleanup_containers(root: Path) -> list[str]:
    """Reject container links before any glob, lock, or recovery-journal scan."""
    errors: list[str] = []
    if os.path.lexists(root):
        if root.is_symlink():
            errors.append(f"project root must not be a symbolic link: {root}")
        elif not root.is_dir():
            errors.append(f"project root is not a directory: {root}")
    else:
        errors.append(f"project root does not exist: {root}")

    for directory_name in TRUSTED_CONTAINER_DIRS:
        directory = root / directory_name
        if not os.path.lexists(directory):
            continue
        if directory.is_symlink():
            errors.append(
                f"cleanup container must not be a symbolic link: {directory}"
            )
        elif not directory.is_dir():
            errors.append(f"cleanup container is not a directory: {directory}")
    return errors


def validate_target_ancestors(root: Path, target: Path) -> None:
    """Ensure every existing ancestor is a real directory.

    The leaf is deliberately excluded: deleting a leaf symlink with ``unlink``
    is safe and is part of the cleanup command's documented behavior.
    """
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise CleanupSafetyError(f"cleanup target is outside the project: {target}") from exc

    ancestor = root
    for segment in relative.parts[:-1]:
        ancestor /= segment
        if not os.path.lexists(ancestor):
            continue
        if ancestor.is_symlink():
            raise CleanupSafetyError(
                f"cleanup target has a symbolic-link ancestor: {ancestor} (target: {target})"
            )
        if not ancestor.is_dir():
            raise CleanupSafetyError(
                f"cleanup target has a non-directory ancestor: {ancestor} (target: {target})"
            )


def pycache_targets(root: Path) -> list[Path]:
    """Find cache directories without following directory symlinks."""
    targets: list[Path] = []
    for directory_name in ("src", "scripts", "tests"):
        search_root = root / directory_name
        if not search_root.is_dir() or search_root.is_symlink():
            continue
        for current, directories, _files in os.walk(search_root, followlinks=False):
            current_path = Path(current)
            retained: list[str] = []
            for name in directories:
                candidate = current_path / name
                if name == "__pycache__":
                    targets.append(candidate)
                    continue
                if candidate.is_symlink():
                    continue
                retained.append(name)
            directories[:] = retained
    return targets


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
        *pycache_targets(root),
    ]
    if drop_manifest:
        targets.insert(9, runs_root / "task-manifest.jsonl")
    return targets


def active_run_markers(root: Path) -> list[Path]:
    sessions_root = root / "runs/claude-sessions"
    validate_target_ancestors(root, sessions_root)
    if sessions_root.is_symlink() or not sessions_root.is_dir():
        return []
    return sorted(sessions_root.rglob(ACTIVE_RUN_MARKER))


def pending_transaction_files(root: Path) -> list[Path]:
    pending: list[Path] = []
    for directory_name in TRANSACTION_DIRS:
        directory = root / "runs" / directory_name
        validate_target_ancestors(root, directory)
        if directory.is_symlink() or not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if path.name != ".gitkeep":
                pending.append(path)
    return sorted(pending)


def acquire_cleanup_lock(root: Path) -> TextIO | None:
    lock_path = root / "runs" / ACTIVE_RUN_LOCK
    validate_target_ancestors(root, lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    except BaseException:
        handle.close()
        raise
    return handle


def restore_skeleton(root: Path) -> None:
    runs_root = root / "runs"
    for rel_path in RUN_SKELETON_DIRS:
        skeleton_dir = runs_root / rel_path
        validate_target_ancestors(root, skeleton_dir)
        skeleton_dir.mkdir(parents=True, exist_ok=True)
        (skeleton_dir / ".gitkeep").write_text(GITKEEP_TEXT, encoding="utf-8")


def remove_target(target: Path, *, root: Path | None = None) -> None:
    if root is not None:
        validate_target_ancestors(root, target)
    if not os.path.lexists(target):
        return
    if target.is_symlink() or not target.is_dir():
        target.unlink()
    else:
        shutil.rmtree(target)


def command_clean(args: argparse.Namespace) -> int:
    root = project_root()
    try:
        safety_errors = validate_cleanup_containers(root)
        if safety_errors:
            print("refusing unsafe cleanup path configuration", file=sys.stderr)
            for error in safety_errors:
                print(f"- {error}", file=sys.stderr)
            return 1
        targets = clean_targets(root, drop_manifest=args.drop_manifest)
        for target in targets:
            validate_target_ancestors(root, target)
        pending_transactions = pending_transaction_files(root)
    except (CleanupSafetyError, OSError) as exc:
        print(f"refusing cleanup because paths could not be inspected safely: {exc}", file=sys.stderr)
        return 1

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

    cleanup_lock: TextIO | None = None
    try:
        cleanup_lock = acquire_cleanup_lock(root)
        markers = active_run_markers(root)
    except (CleanupSafetyError, OSError) as exc:
        if cleanup_lock is not None:
            fcntl.flock(cleanup_lock.fileno(), fcntl.LOCK_UN)
            cleanup_lock.close()
        print(f"refusing cleanup because paths could not be locked safely: {exc}", file=sys.stderr)
        return 1
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

    try:
        pending_transactions = pending_transaction_files(root)
    except (CleanupSafetyError, OSError) as exc:
        print(f"cleanup stopped because recovery paths became unsafe: {exc}", file=sys.stderr)
        if cleanup_lock is not None:
            fcntl.flock(cleanup_lock.fileno(), fcntl.LOCK_UN)
            cleanup_lock.close()
        return 1
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
            remove_target(target, root=root)
        restore_skeleton(root)
    except (CleanupSafetyError, OSError) as exc:
        print(f"cleanup stopped because a path became unsafe: {exc}", file=sys.stderr)
        return 1
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

#!/usr/bin/env python3
"""Phase 7 runner and validator: finalize ready or rejected tasks."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any, Iterator

from taskgen.common import (
    ValidationReport,
    append_jsonl_object,
    fsync_parent_directory,
    fsync_path_tree,
    load_json,
    phase_subject_lock,
    print_report,
    project_root,
    read_jsonl_objects,
    require_object,
    write_json_object_atomic,
)
from taskgen.phases.phase3_task_generation import (
    generated_task_ref_for,
    validate_no_forbidden_text,
    validate_no_runner_artifacts,
    validate_idea_id,
    validate_required_task_layout,
    validate_seed_id,
)
from taskgen.phases.phase4_oracle_nop_check import validate_phase4, validate_status_payload
from taskgen.phases.phase5_task_review import (
    review_json_path_for,
    review_markdown_path_for,
    subject_for,
    validate_review_markdown,
    validate_review_payload,
    validate_phase5,
)


PHASE_KEY = "phase7"
FINAL_DECISIONS = {"ready", "rejected"}


def working_task_path(root: Path, seed_id: str, idea_id: str) -> Path:
    return root / generated_task_ref_for(seed_id, idea_id)


def accepted_task_path(root: Path, seed_id: str, idea_id: str) -> Path:
    return root / "generated/accepted" / subject_for(seed_id, idea_id)


def rejected_task_path(root: Path, seed_id: str, idea_id: str) -> Path:
    return root / "generated/rejected" / subject_for(seed_id, idea_id)


def oracle_status_ref(seed_id: str, idea_id: str) -> str:
    return f"runs/oracle-nop-check/{subject_for(seed_id, idea_id)}/oracle-nop-status.json"


def review_ref(seed_id: str, idea_id: str) -> str:
    return f"runs/reviews/{subject_for(seed_id, idea_id)}/review.json"


def oracle_status_path_for(root: Path, seed_id: str, idea_id: str) -> Path:
    return root / oracle_status_ref(seed_id, idea_id)


def load_review_decision(root: Path, seed_id: str, idea_id: str, report: ValidationReport) -> str | None:
    data = load_json(review_json_path_for(root, seed_id, idea_id), report)
    review = require_object(data, "$", report) if data is not None else None
    if review is None:
        return None

    decision = review.get("decision")
    if not isinstance(decision, str) or not decision.strip():
        report.errors.append("$.decision must be a non-empty string")
        return None
    return decision


def validate_final_review(root: Path, seed_id: str, idea_id: str, report: ValidationReport) -> str | None:
    task_id = subject_for(seed_id, idea_id)
    review_json_path = review_json_path_for(root, seed_id, idea_id)
    review_md_path = review_markdown_path_for(root, seed_id, idea_id)

    payload = load_json(review_json_path, report)
    review = require_object(payload, "$", report) if payload is not None else None
    decision: str | None = None
    if review is not None:
        validate_review_payload(review, task_id, report)
        raw_decision = review.get("decision")
        if isinstance(raw_decision, str):
            decision = raw_decision

    report.checked_paths.append(str(review_md_path))
    if not review_md_path.is_file():
        report.errors.append(f"missing review markdown: {review_md_path}")
    else:
        validate_review_markdown(review_md_path, report, review)
    return decision


def validate_final_oracle_status(
    root: Path,
    seed_id: str,
    idea_id: str,
    finalized_task_path: Path,
    report: ValidationReport,
    *,
    require_passed: bool,
) -> None:
    status_path = oracle_status_path_for(root, seed_id, idea_id)
    data = load_json(status_path, report)
    payload = require_object(data, "$.oracle_nop", report) if data is not None else None
    if payload is None:
        return

    validate_status_payload(
        root,
        finalized_task_path,
        subject_for(seed_id, idea_id),
        payload,
        report,
        require_passed=require_passed,
        expected_status_task_path=working_task_path(root, seed_id, idea_id),
    )


def validate_phase7(
    root: Path,
    seed_id: str,
    idea_id: str,
    *,
    require_manifest: bool = True,
    require_no_pending_transaction: bool = True,
) -> ValidationReport:
    report = ValidationReport(phase=PHASE_KEY, seed_id=seed_id)
    id_errors = validate_seed_id(seed_id) + validate_idea_id(idea_id)
    if id_errors:
        report.errors.extend(id_errors)
        return report

    decision = validate_final_review(root, seed_id, idea_id, report)
    if decision == "needs_modification":
        report.errors.append("review decision is 'needs_modification'; run phase6 before phase7")
        return report
    if decision not in FINAL_DECISIONS:
        report.errors.append("review decision must be 'ready' or 'rejected' for phase7")
        return report

    accepted = accepted_task_path(root, seed_id, idea_id)
    rejected = rejected_task_path(root, seed_id, idea_id)
    if decision == "ready":
        validate_final_task_dir(accepted, "accepted task", report, seed_id=seed_id)
        validate_final_oracle_status(
            root,
            seed_id,
            idea_id,
            accepted,
            report,
            require_passed=True,
        )
        if os.path.lexists(rejected):
            report.errors.append(f"rejected task directory must not exist for ready decision: {rejected}")
    else:
        validate_final_task_dir(rejected, "rejected task", report, seed_id=seed_id)
        validate_final_oracle_status(
            root,
            seed_id,
            idea_id,
            rejected,
            report,
            require_passed=False,
        )
        if os.path.lexists(accepted):
            report.errors.append(f"accepted task directory must not exist for rejected decision: {accepted}")

    source = working_task_path(root, seed_id, idea_id)
    report.checked_paths.append(str(source))
    if os.path.lexists(source):
        report.errors.append(f"working task directory must be removed after phase7 finalization: {source}")

    if require_no_pending_transaction:
        journal_path = finalization_journal_path(root, source)
        report.checked_paths.append(str(journal_path))
        if journal_path.exists():
            report.errors.append(
                f"phase7 has a pending recovery journal; rerun phase7: {journal_path}"
            )

    if require_manifest:
        validate_manifest_event(root, seed_id, idea_id, decision, report)
    return report


def validate_final_task_dir(
    path: Path,
    label: str,
    report: ValidationReport,
    *,
    seed_id: str | None = None,
) -> None:
    before = len(report.errors)
    validate_required_task_layout(path, report)
    if not path.is_dir():
        if len(report.errors) == before:
            report.errors.append(f"missing {label} directory: {path}")
        return
    validate_no_runner_artifacts(path, report)
    if seed_id is not None:
        validate_no_forbidden_text(path, seed_id, report)


def validate_manifest_event(
    root: Path,
    seed_id: str,
    idea_id: str,
    decision: str,
    report: ValidationReport,
) -> None:
    manifest_path = root / "runs/task-manifest.jsonl"
    report.checked_paths.append(str(manifest_path))
    if not manifest_path.is_file():
        report.errors.append(f"missing manifest: {manifest_path}")
        return

    expected_event = "accepted" if decision == "ready" else "rejected"
    expected_task_id = subject_for(seed_id, idea_id)
    expected_task_path = (
        f"generated/accepted/{expected_task_id}"
        if decision == "ready"
        else f"generated/rejected/{expected_task_id}"
    )
    status_data = load_json(oracle_status_path_for(root, seed_id, idea_id), report)
    status_payload = require_object(status_data, "$.oracle_nop_manifest", report) if status_data is not None else None
    expected_run_id = status_payload.get("run_id") if status_payload is not None else None
    expected_hash = status_payload.get("task_tree_sha256") if status_payload is not None else None
    found = False
    for _line_number, event in read_jsonl_objects(manifest_path, report):
        if (
            event.get("event") == expected_event
            and event.get("seed_id") == seed_id
            and event.get("idea_id") == idea_id
            and event.get("task_id") == expected_task_id
            and event.get("task_path") == expected_task_path
            and event.get("review_ref") == review_ref(seed_id, idea_id)
            and event.get("oracle_nop_ref") == oracle_status_ref(seed_id, idea_id)
            and event.get("source_task_ref") == generated_task_ref_for(seed_id, idea_id)
            and event.get("run_id") == expected_run_id
            and event.get("task_tree_sha256") == expected_hash
            and event.get("status") == expected_event
        ):
            found = True
            break

    if not found:
        report.errors.append(
            f"manifest has no matching {expected_event} event for task_id={expected_task_id!r}"
        )


def ensure_phase7_inputs(root: Path, seed_id: str, idea_id: str) -> list[str]:
    errors = validate_seed_id(seed_id) + validate_idea_id(idea_id)
    if errors:
        return errors

    decision_report = ValidationReport(phase=PHASE_KEY, seed_id=seed_id)
    decision = load_review_decision(root, seed_id, idea_id, decision_report)
    errors.extend(decision_report.errors)
    if decision == "needs_modification":
        errors.append("phase7 cannot run while review decision is 'needs_modification'; run phase6 first")
    elif decision not in FINAL_DECISIONS:
        errors.append(f"phase7 requires review decision 'ready' or 'rejected', got {decision!r}")
    if errors:
        return errors

    report = validate_phase5(root, seed_id, idea_id)
    if not report.passed:
        errors.append("phase5 validation must pass before phase7")
        errors.extend(report.errors)
        return errors

    phase4_report = validate_phase4(
        root,
        seed_id,
        idea_id,
        require_passed=decision == "ready",
    )
    if not phase4_report.passed:
        if decision == "ready":
            errors.append("phase7 requires phase4 oracle/nop to pass for a ready task")
        else:
            errors.append("phase7 requires a reviewable phase4 status for a rejected task")
        errors.extend(phase4_report.errors)
    return errors


class FinalizationError(RuntimeError):
    pass


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _remove_path(path: Path) -> None:
    if not _path_exists(path):
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def require_safe_finalization_paths(root: Path, *paths: tuple[Path, str]) -> None:
    """Reject finalization paths that escape the project or traverse symlinks."""
    try:
        resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FinalizationError(f"cannot resolve project root safely: {exc}") from exc

    for candidate, label in paths:
        lexical = Path(os.path.abspath(candidate))
        try:
            relative = lexical.relative_to(resolved_root)
        except ValueError:
            raise FinalizationError(f"{label} must stay inside project root: {candidate}") from None

        current = resolved_root
        for part in relative.parts:
            current /= part
            try:
                if current.is_symlink():
                    raise FinalizationError(f"{label} must not traverse symlink: {current}")
            except OSError as exc:
                raise FinalizationError(f"cannot inspect {label} path {current}: {exc}") from exc

        try:
            resolved_candidate = lexical.resolve(strict=False)
            resolved_candidate.relative_to(resolved_root)
        except (OSError, ValueError, RuntimeError) as exc:
            raise FinalizationError(
                f"cannot resolve {label} safely inside project root: {candidate}"
            ) from exc


def finalization_journal_path(root: Path, source: Path) -> Path:
    try:
        source_key = Path(os.path.abspath(source)).relative_to(root.resolve()).as_posix()
    except (OSError, ValueError, RuntimeError):
        source_key = str(Path(os.path.abspath(source)))
    digest = hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:24]
    return root / "runs/finalization-transactions" / f"{digest}.json"


def fsync_finalization_directories(
    source: Path,
    destination: Path,
    counterpart: Path,
) -> None:
    for path in (source, destination, counterpart):
        fsync_parent_directory(path)


def _journal_path_value(payload: dict[str, Any], key: str) -> Path:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise FinalizationError(f"phase7 recovery journal has invalid {key!r}")
    return Path(value)


def _require_expected_recovery_temp(path: Path, parent: Path, prefix: str, label: str) -> None:
    lexical = Path(os.path.abspath(path))
    if lexical.parent != Path(os.path.abspath(parent)) or not re.fullmatch(
        rf"{re.escape(prefix)}[0-9a-f]{{32}}",
        lexical.name,
    ):
        raise FinalizationError(f"phase7 recovery journal has unsafe {label}: {path}")


def recover_interrupted_finalization(
    root: Path,
    source: Path,
    destination: Path,
    counterpart: Path,
    *,
    seed_id: str,
    idea_id: str,
    require_passed: bool = True,
) -> None:
    """Finish or roll back a previously interrupted rename transaction."""
    journal_path = finalization_journal_path(root, source)
    require_safe_finalization_paths(root, (journal_path, "phase7 recovery journal"))
    if not journal_path.is_file():
        return
    try:
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FinalizationError(f"cannot read phase7 recovery journal {journal_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise FinalizationError(f"phase7 recovery journal must contain an object: {journal_path}")

    recorded_source = _journal_path_value(payload, "source")
    recorded_destination = _journal_path_value(payload, "destination")
    recorded_counterpart = _journal_path_value(payload, "counterpart")
    expected_paths = tuple(Path(os.path.abspath(path)) for path in (source, destination, counterpart))
    recorded_paths = tuple(
        Path(os.path.abspath(path))
        for path in (recorded_source, recorded_destination, recorded_counterpart)
    )
    if recorded_paths != expected_paths:
        raise FinalizationError(
            "phase7 recovery journal does not match the current source/destination decision"
        )

    stage = _journal_path_value(payload, "stage")
    destination_backup = _journal_path_value(payload, "destination_backup")
    counterpart_backup = _journal_path_value(payload, "counterpart_backup")
    source_backup = _journal_path_value(payload, "source_backup")
    _require_expected_recovery_temp(stage, destination.parent, ".taskgen-stage-", "stage")
    _require_expected_recovery_temp(
        destination_backup,
        destination.parent,
        ".taskgen-final-backup-",
        "destination backup",
    )
    _require_expected_recovery_temp(
        counterpart_backup,
        counterpart.parent,
        ".taskgen-counterpart-backup-",
        "counterpart backup",
    )
    _require_expected_recovery_temp(
        source_backup,
        source.parent,
        ".taskgen-working-backup-",
        "working backup",
    )
    require_safe_finalization_paths(
        root,
        (stage, "phase7 recovery stage"),
        (destination_backup, "phase7 destination backup"),
        (counterpart_backup, "phase7 counterpart backup"),
        (source_backup, "phase7 working backup"),
    )

    state = payload.get("state")
    if state not in {"preparing", "switched", "committed"}:
        raise FinalizationError(f"phase7 recovery journal has invalid state: {state!r}")
    if not isinstance(payload.get("destination_existed"), bool) or not isinstance(
        payload.get("counterpart_existed"), bool
    ):
        raise FinalizationError("phase7 recovery journal has invalid existence flags")
    switch_complete = (
        not _path_exists(source)
        and destination.is_dir()
        and not destination.is_symlink()
    )
    if switch_complete:
        recovery_report = ValidationReport(phase=PHASE_KEY, seed_id=seed_id)
        validate_final_task_dir(
            destination,
            "recovered final task",
            recovery_report,
            seed_id=seed_id,
        )
        validate_final_oracle_status(
            root,
            seed_id,
            idea_id,
            destination,
            recovery_report,
            require_passed=require_passed,
        )
        if recovery_report.errors:
            switch_complete = False
            print(
                "phase7 recovery: final destination validation failed; rolling back",
                file=sys.stderr,
            )
            for error in recovery_report.errors:
                print(f"- {error}", file=sys.stderr)
    if switch_complete:
        if not destination.is_dir() or destination.is_symlink():
            raise FinalizationError(
                "phase7 recovery cannot commit because the final destination is missing or unsafe"
            )
        for temporary in (
            source_backup,
            destination_backup,
            counterpart_backup,
            stage,
        ):
            _remove_path(temporary)
        fsync_finalization_directories(source, destination, counterpart)
        journal_path.unlink(missing_ok=True)
        fsync_parent_directory(journal_path)
        print(f"phase7 recovery: completed interrupted final switch at {destination}")
        return

    rollback_errors: list[str] = []
    destination_existed = payload.get("destination_existed") is True
    counterpart_existed = payload.get("counterpart_existed") is True
    source_exists = _path_exists(source)
    source_backup_exists = _path_exists(source_backup)
    destination_exists = _path_exists(destination)
    destination_backup_exists = _path_exists(destination_backup)
    counterpart_exists = _path_exists(counterpart)
    counterpart_backup_exists = _path_exists(counterpart_backup)
    if not source_exists and not source_backup_exists:
        rollback_errors.append("working task and its recovery backup are both missing")
    if source_exists and source_backup_exists:
        rollback_errors.append("working task and its recovery backup both exist")
    if state == "committed" and source_exists:
        rollback_errors.append("committed finalization unexpectedly retains a working task")
    if destination_existed:
        if not destination_backup_exists and not destination_exists:
            rollback_errors.append("final destination and its recovery backup are both missing")
        elif state in {"switched", "committed"} and not destination_backup_exists:
            rollback_errors.append("original final destination recovery backup is unavailable")
    elif destination_backup_exists:
        rollback_errors.append("unexpected final destination recovery backup exists")
    if counterpart_existed:
        if not counterpart_backup_exists and not counterpart_exists:
            rollback_errors.append("final counterpart and its recovery backup are both missing")
        elif state in {"switched", "committed"} and not counterpart_backup_exists:
            rollback_errors.append("original final counterpart recovery backup is unavailable")
    elif counterpart_backup_exists or counterpart_exists:
        rollback_errors.append("unexpected final counterpart recovery path exists")
    if rollback_errors:
        raise FinalizationError(
            "phase7 interrupted transaction cannot be rolled back safely: "
            + "; ".join(rollback_errors)
        )

    try:
        if source_backup_exists:
            os.replace(source_backup, source)

        if destination_existed:
            if _path_exists(destination_backup):
                _remove_path(destination)
                os.replace(destination_backup, destination)
        else:
            _remove_path(destination)

        if counterpart_existed:
            if _path_exists(counterpart_backup):
                _remove_path(counterpart)
                os.replace(counterpart_backup, counterpart)
        else:
            _remove_path(counterpart_backup)
        _remove_path(stage)
    except OSError as exc:
        rollback_errors.append(str(exc))

    if rollback_errors:
        raise FinalizationError(
            "phase7 interrupted-transaction rollback was incomplete: "
            + "; ".join(rollback_errors)
        )
    try:
        fsync_finalization_directories(source, destination, counterpart)
        journal_path.unlink(missing_ok=True)
        fsync_parent_directory(journal_path)
    except OSError as exc:
        raise FinalizationError(
            f"cannot persist phase7 interrupted-transaction recovery: {exc}"
        ) from exc
    print("phase7 recovery: rolled back an interrupted final switch")


@contextlib.contextmanager
def final_task_transaction(
    source: Path,
    destination: Path,
    counterpart: Path,
    *,
    seed_id: str | None = None,
    project_root_path: Path | None = None,
) -> Iterator[None]:
    """Stage and atomically switch final paths, restoring originals on failure."""
    if project_root_path is not None:
        require_safe_finalization_paths(
            project_root_path,
            (source, "working task source"),
            (destination, "final task destination"),
            (counterpart, "final task counterpart"),
        )
    if not source.is_dir():
        raise FinalizationError(f"working task source is not a directory: {source}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    counterpart.parent.mkdir(parents=True, exist_ok=True)
    nonce = uuid.uuid4().hex
    stage = destination.parent / f".taskgen-stage-{nonce}"
    destination_backup = destination.parent / f".taskgen-final-backup-{nonce}"
    counterpart_backup = counterpart.parent / f".taskgen-counterpart-backup-{nonce}"
    source_backup = source.parent / f".taskgen-working-backup-{nonce}"
    destination_installed = False
    journal_path: Path | None = None
    journal_payload: dict[str, Any] | None = None

    if project_root_path is not None:
        journal_path = finalization_journal_path(project_root_path, source)
        require_safe_finalization_paths(
            project_root_path,
            (journal_path, "phase7 transaction journal"),
        )
        journal_payload = {
            "state": "preparing",
            "source": str(Path(os.path.abspath(source))),
            "destination": str(Path(os.path.abspath(destination))),
            "counterpart": str(Path(os.path.abspath(counterpart))),
            "stage": str(stage),
            "destination_backup": str(destination_backup),
            "counterpart_backup": str(counterpart_backup),
            "source_backup": str(source_backup),
            "destination_existed": _path_exists(destination),
            "counterpart_existed": _path_exists(counterpart),
        }
        write_json_object_atomic(journal_path, journal_payload)

    try:
        shutil.copytree(source, stage, symlinks=True)
        stage_report = ValidationReport(phase=PHASE_KEY, seed_id=seed_id or "")
        validate_final_task_dir(stage, "staged final task", stage_report, seed_id=seed_id)
        if stage_report.errors:
            raise FinalizationError(
                "staged final task failed cleanliness validation: "
                + "; ".join(stage_report.errors)
            )
        fsync_path_tree(stage)

        if _path_exists(destination):
            os.replace(destination, destination_backup)
        if _path_exists(counterpart):
            os.replace(counterpart, counterpart_backup)
        os.replace(stage, destination)
        destination_installed = True
        os.replace(source, source_backup)
        for switched_path in (source, destination, counterpart):
            fsync_parent_directory(switched_path)
        if journal_path is not None and journal_payload is not None:
            journal_payload["state"] = "switched"
            write_json_object_atomic(journal_path, journal_payload)
        yield
    except BaseException as exc:
        rollback_errors: list[str] = []
        for action in (
            lambda: os.replace(source_backup, source) if _path_exists(source_backup) else None,
            lambda: _remove_path(destination) if destination_installed else None,
            lambda: os.replace(destination_backup, destination)
            if _path_exists(destination_backup)
            else None,
            lambda: os.replace(counterpart_backup, counterpart)
            if _path_exists(counterpart_backup)
            else None,
            lambda: _remove_path(stage),
        ):
            try:
                action()
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if not rollback_errors:
            try:
                fsync_finalization_directories(source, destination, counterpart)
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if rollback_errors:
            raise FinalizationError(
                f"phase7 failed ({exc}) and rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from exc
        if journal_path is not None:
            journal_path.unlink(missing_ok=True)
            fsync_parent_directory(journal_path)
        raise
    else:
        if journal_path is not None and journal_payload is not None:
            journal_payload["state"] = "committed"
            try:
                write_json_object_atomic(journal_path, journal_payload)
            except OSError as journal_exc:
                print(
                    f"phase7 warning: cannot mark transaction journal committed: {journal_exc}",
                    file=sys.stderr,
                )
        cleanup_failed = False
        for backup in (source_backup, destination_backup, counterpart_backup, stage):
            try:
                _remove_path(backup)
            except OSError as cleanup_exc:
                cleanup_failed = True
                print(f"phase7 warning: cannot remove transaction backup {backup}: {cleanup_exc}", file=sys.stderr)
        if not cleanup_failed:
            try:
                fsync_finalization_directories(source, destination, counterpart)
            except OSError as cleanup_exc:
                cleanup_failed = True
                print(
                    f"phase7 warning: cannot persist transaction cleanup: {cleanup_exc}",
                    file=sys.stderr,
                )
        if journal_path is not None and not cleanup_failed:
            try:
                journal_path.unlink(missing_ok=True)
                fsync_parent_directory(journal_path)
            except OSError as cleanup_exc:
                print(
                    f"phase7 warning: cannot remove transaction journal {journal_path}: {cleanup_exc}",
                    file=sys.stderr,
                )


def move_final_task(source: Path, destination: Path, counterpart: Path) -> None:
    common_root = Path(
        os.path.commonpath(
            [
                Path(os.path.abspath(source)),
                Path(os.path.abspath(destination)),
                Path(os.path.abspath(counterpart)),
            ]
        )
    )
    containment_root = common_root.parent if common_root.name == "generated" else common_root
    require_safe_finalization_paths(
        containment_root,
        (source, "working task source"),
        (destination, "final task destination"),
        (counterpart, "final task counterpart"),
    )
    journal_root = containment_root if common_root.name == "generated" else None
    with final_task_transaction(
        source,
        destination,
        counterpart,
        project_root_path=journal_root,
    ):
        pass


def append_manifest_event(root: Path, seed_id: str, idea_id: str, decision: str) -> None:
    event_name = "accepted" if decision == "ready" else "rejected"
    task_id = subject_for(seed_id, idea_id)
    task_path = (
        f"generated/accepted/{task_id}"
        if decision == "ready"
        else f"generated/rejected/{task_id}"
    )
    payload: dict[str, Any] = {
        "event": event_name,
        "seed_id": seed_id,
        "idea_id": idea_id,
        "task_id": task_id,
        "task_path": task_path,
        "source_task_ref": generated_task_ref_for(seed_id, idea_id),
        "review_ref": review_ref(seed_id, idea_id),
        "oracle_nop_ref": oracle_status_ref(seed_id, idea_id),
        "status": event_name,
        "reason": f"phase 7 finalized review decision {decision!r}",
    }
    try:
        status_data = load_json_for_manifest(oracle_status_path_for(root, seed_id, idea_id))
    except (OSError, ValueError) as exc:
        raise FinalizationError(f"cannot read oracle/nop status for final manifest: {exc}") from exc
    payload["run_id"] = status_data.get("run_id")
    payload["task_tree_sha256"] = status_data.get("task_tree_sha256")
    manifest_path = root / "runs/task-manifest.jsonl"
    append_jsonl_object(manifest_path, payload)


def load_json_for_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def command_run(args: argparse.Namespace) -> int:
    root = project_root()
    id_errors = validate_seed_id(args.seed_id) + validate_idea_id(args.idea_id)
    if id_errors:
        for error in id_errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    task_id = subject_for(args.seed_id, args.idea_id)
    with phase_subject_lock(root, PHASE_KEY, task_id):
        return run_phase7_locked(root, args)


def run_phase7_locked(root: Path, args: argparse.Namespace) -> int:
    decision_report = ValidationReport(phase=PHASE_KEY, seed_id=args.seed_id)
    decision = load_review_decision(root, args.seed_id, args.idea_id, decision_report)
    if decision_report.errors or decision not in FINAL_DECISIONS:
        for error in decision_report.errors:
            print(f"- {error}", file=sys.stderr)
        if decision not in FINAL_DECISIONS:
            print(f"- phase7 requires decision 'ready' or 'rejected', got {decision!r}", file=sys.stderr)
        return 1

    source = working_task_path(root, args.seed_id, args.idea_id)
    if decision == "ready":
        destination = accepted_task_path(root, args.seed_id, args.idea_id)
        counterpart = rejected_task_path(root, args.seed_id, args.idea_id)
    else:
        destination = rejected_task_path(root, args.seed_id, args.idea_id)
        counterpart = accepted_task_path(root, args.seed_id, args.idea_id)

    try:
        require_safe_finalization_paths(
            root,
            (source, "working task source"),
            (destination, "final task destination"),
            (counterpart, "final task counterpart"),
        )
    except FinalizationError as exc:
        print(f"cannot run phase7 safely: {exc}", file=sys.stderr)
        return 1

    try:
        recover_interrupted_finalization(
            root,
            source,
            destination,
            counterpart,
            seed_id=args.seed_id,
            idea_id=args.idea_id,
            require_passed=decision == "ready",
        )
    except (OSError, FinalizationError) as exc:
        print(f"phase7 recovery failed: {exc}", file=sys.stderr)
        return 1

    if not source.exists() and destination.is_dir():
        print(f"phase7 recovery: reusing finalized task at {destination}")
        existing_report = validate_phase7(root, args.seed_id, args.idea_id)
        if existing_report.passed:
            return print_report(existing_report, as_json=False)
        reusable_report = validate_phase7(
            root,
            args.seed_id,
            args.idea_id,
            require_manifest=False,
        )
        if not reusable_report.passed:
            return print_report(reusable_report, as_json=False)
        if args.dry_run:
            print("phase7 recovery would append the missing final manifest event")
            return 0
        try:
            append_manifest_event(root, args.seed_id, args.idea_id, decision)
        except (OSError, FinalizationError) as exc:
            print(f"phase7 recovery failed to append manifest: {exc}", file=sys.stderr)
            return 1
        return print_report(validate_phase7(root, args.seed_id, args.idea_id), as_json=False)

    errors = ensure_phase7_inputs(root, args.seed_id, args.idea_id)
    if errors:
        print(
            f"cannot run phase7 for seed {args.seed_id} idea {args.idea_id}; prerequisites failed",
            file=sys.stderr,
        )
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(f"phase7 decision: {decision}")
    print(f"phase7 source: {source}")
    print(f"phase7 destination: {destination}")
    if counterpart.exists():
        print(f"phase7 removes counterpart: {counterpart}")
    if args.dry_run:
        return 0

    try:
        with final_task_transaction(
            source,
            destination,
            counterpart,
            seed_id=args.seed_id,
            project_root_path=root,
        ):
            pre_manifest_report = validate_phase7(
                root,
                args.seed_id,
                args.idea_id,
                require_manifest=False,
                require_no_pending_transaction=False,
            )
            if not pre_manifest_report.passed:
                raise FinalizationError("; ".join(pre_manifest_report.errors))
    except (OSError, FinalizationError) as exc:
        print(f"phase7 finalization failed and was rolled back: {exc}", file=sys.stderr)
        return 1
    try:
        append_manifest_event(root, args.seed_id, args.idea_id, decision)
    except (OSError, FinalizationError) as exc:
        print(
            "phase7 final switch completed, but manifest append failed; "
            f"rerun phase7 to recover: {exc}",
            file=sys.stderr,
        )
        return 1
    return print_report(validate_phase7(root, args.seed_id, args.idea_id), as_json=False)


def command_validate(args: argparse.Namespace) -> int:
    return print_report(validate_phase7(project_root(), args.seed_id, args.idea_id), args.json)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Finalize one ready or rejected task.")
    run.add_argument("seed_id")
    run.add_argument("--idea-id", required=True)
    run.add_argument("--dry-run", action="store_true", help="Print finalization action without copying files.")
    run.set_defaults(func=command_run)

    validate = subparsers.add_parser("validate", help="Validate phase7 finalized output.")
    validate.add_argument("seed_id")
    validate.add_argument("--idea-id", required=True)
    validate.add_argument("--json", action="store_true", help="Emit machine-readable validation output.")
    validate.set_defaults(func=command_validate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

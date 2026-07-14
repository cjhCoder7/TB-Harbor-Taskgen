#!/usr/bin/env python3
"""Shared utilities for TB Harbor task generation scripts."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


SAFE_PATH_SEGMENT_RE = re.compile(r"[A-Za-z0-9._-]+")
TEMPLATE_MARKER_RE = re.compile(r"{{[^{}]+}}")
MAX_SEED_ID_LENGTH = 128
MAX_IDEA_ID_LENGTH = 120
_PHASE_LOCK_STATE = threading.local()
_PHASE_LOCK_DELEGATION_ENV = "_TASKGEN_PARENT_PHASE_SUBJECT_LOCK"


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


def validate_seed_identifier(seed_id: str) -> list[str]:
    """Validate a seed id without permitting the subject-id separator."""
    errors = validate_path_segment(seed_id, "seed_id")
    if len(seed_id) > MAX_SEED_ID_LENGTH:
        errors.append(f"seed_id must be at most {MAX_SEED_ID_LENGTH} characters")
    if "__" in seed_id:
        errors.append("seed_id must not contain reserved separator '__'")
    return errors


def validate_idea_identifier(idea_id: str) -> list[str]:
    errors = validate_path_segment(idea_id, "idea_id")
    if len(idea_id) > MAX_IDEA_ID_LENGTH:
        errors.append(f"idea_id must be at most {MAX_IDEA_ID_LENGTH} characters")
    if "__" in idea_id:
        errors.append("idea_id must not contain reserved separator '__'")
    return errors


def require_no_template_markers(rendered: str, label: str) -> None:
    markers = sorted(set(TEMPLATE_MARKER_RE.findall(rendered)))
    if markers:
        raise SystemExit(f"{label} contains unreplaced marker(s): {', '.join(markers)}")


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
    except (OSError, UnicodeError) as exc:
        report.errors.append(f"cannot read JSON file {path}: {exc}")
        return None
    except json.JSONDecodeError as exc:
        report.errors.append(f"invalid JSON in {path}: {exc}")
        return None


def read_jsonl_objects(
    path: Path,
    report: ValidationReport,
) -> list[tuple[int, dict[str, Any]]]:
    """Read a JSONL file defensively, reporting malformed/non-object records."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        report.errors.append(f"cannot read manifest {path}: {exc}")
        return []

    records: list[tuple[int, dict[str, Any]]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            report.errors.append(f"invalid JSONL at {path}:{line_number}: {exc}")
            continue
        if not isinstance(value, dict):
            report.errors.append(f"manifest record must be a JSON object at {path}:{line_number}")
            continue
        records.append((line_number, value))
    return records


def append_jsonl_object(path: Path, payload: dict[str, Any]) -> None:
    """Append one JSONL object under an advisory lock and roll back short writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    existed_before = os.path.lexists(path)
    open_flags = (
        os.O_RDWR
        | os.O_APPEND
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_descriptor = os.open(path, open_flags, 0o666)
    try:
        handle = os.fdopen(file_descriptor, "a+b", buffering=0)
    except BaseException:
        os.close(file_descriptor)
        raise
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        if not existed_before:
            fsync_parent_directory(path)
        handle.seek(0, os.SEEK_END)
        start = handle.tell()
        try:
            written = handle.write(encoded)
            if written != len(encoded):
                raise OSError(
                    f"short manifest append to {path}: wrote {written} of {len(encoded)} bytes"
                )
            os.fsync(handle.fileno())
        except BaseException:
            try:
                os.ftruncate(handle.fileno(), start)
                os.fsync(handle.fileno())
            except OSError:
                pass
            raise
    finally:
        # The append is committed once fsync succeeds. Unlock/close errors must
        # not make callers roll back filesystem state after a durable event.
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            handle.close()
        except OSError:
            pass


def write_json_object_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Durably replace a JSON object without exposing a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_parent_directory(path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def fsync_parent_directory(path: Path) -> None:
    """Persist directory-entry changes for ``path`` on the POSIX runtime."""
    fsync_directory(path.parent)


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def fsync_path_tree(path: Path) -> None:
    """Persist a regular file or a symlink-free directory tree bottom-up."""
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        raise OSError(f"cannot fsync symbolic link tree: {path}")
    if stat.S_ISREG(metadata.st_mode):
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        file_descriptor = os.open(path, flags)
        try:
            os.fsync(file_descriptor)
        finally:
            os.close(file_descriptor)
        return
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError(f"cannot fsync non-regular output: {path}")

    directories: list[Path] = [path]
    for child in sorted(path.rglob("*")):
        child_metadata = child.lstat()
        if stat.S_ISLNK(child_metadata.st_mode):
            raise OSError(f"cannot fsync tree containing symbolic link: {child}")
        if stat.S_ISREG(child_metadata.st_mode):
            fsync_path_tree(child)
        elif stat.S_ISDIR(child_metadata.st_mode):
            directories.append(child)
        else:
            raise OSError(f"cannot fsync tree containing special file: {child}")
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        fsync_directory(directory)


@contextlib.contextmanager
def pipeline_activity_lock(root: Path) -> Iterator[int]:
    """Mark a run as active so cleanup cannot race workflow mutations."""
    lock_path = root / "runs/.active-runs.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            yield handle.fileno()
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


@contextlib.contextmanager
def phase_subject_lock(root: Path, phase: str, subject: str) -> Iterator[None]:
    """Serialize one seed/task subject across phases while marking activity."""
    key = (str(root.resolve()), subject)
    held = getattr(_PHASE_LOCK_STATE, "held", set())
    if key in held:
        yield
        return
    digest = hashlib.sha256(subject.encode("utf-8")).hexdigest()[:24]
    lock_path = root / "runs/locks" / f"subject-{digest}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with pipeline_activity_lock(root) as activity_fd, lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        held.add(key)
        _PHASE_LOCK_STATE.held = held
        held_fds = getattr(_PHASE_LOCK_STATE, "held_fds", {})
        held_fds[key] = (activity_fd, handle.fileno())
        _PHASE_LOCK_STATE.held_fds = held_fds
        try:
            yield
        finally:
            held_fds.pop(key, None)
            held.remove(key)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


def delegated_phase_subject_lock_kwargs(root: Path, subject: str) -> dict[str, Any]:
    """Pass the parent's actual locked descriptors to its direct runner child."""
    key = (str(root.resolve()), subject)
    held_fds = getattr(_PHASE_LOCK_STATE, "held_fds", {})
    descriptors = held_fds.get(key)
    if descriptors is None:
        raise RuntimeError(f"phase subject lock is not held for delegation: {subject}")
    activity_fd, subject_fd = descriptors
    environment = os.environ.copy()
    environment[_PHASE_LOCK_DELEGATION_ENV] = json.dumps(
        {
            "root": str(root.resolve()),
            "subject": subject,
            "activity_fd": activity_fd,
            "subject_fd": subject_fd,
        },
        separators=(",", ":"),
    )
    return {
        "env": environment,
        "pass_fds": (activity_fd, subject_fd),
    }


def phase_subject_lock_delegated_by_parent(root: Path, subject: str) -> bool:
    """Validate and retain inherited activity/subject locks for this runner."""
    raw_payload = os.environ.get(_PHASE_LOCK_DELEGATION_ENV)
    if not raw_payload:
        return False
    try:
        payload = json.loads(raw_payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("invalid inherited phase subject lock delegation") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "root",
        "subject",
        "activity_fd",
        "subject_fd",
    }:
        raise RuntimeError("invalid inherited phase subject lock delegation")
    if (
        payload.get("root") != str(root.resolve())
        or payload.get("subject") != subject
    ):
        raise RuntimeError("inherited phase subject lock does not match this run")
    activity_fd = payload.get("activity_fd")
    subject_fd = payload.get("subject_fd")
    if (
        not isinstance(activity_fd, int)
        or isinstance(activity_fd, bool)
        or not isinstance(subject_fd, int)
        or isinstance(subject_fd, bool)
        or activity_fd < 0
        or subject_fd < 0
        or activity_fd == subject_fd
    ):
        raise RuntimeError("invalid inherited phase subject lock descriptors")
    digest = hashlib.sha256(subject.encode("utf-8")).hexdigest()[:24]
    expected_paths = (
        root / "runs/.active-runs.lock",
        root / "runs/locks" / f"subject-{digest}.lock",
    )
    try:
        for descriptor, expected_path in zip(
            (activity_fd, subject_fd),
            expected_paths,
        ):
            descriptor_metadata = os.fstat(descriptor)
            path_metadata = expected_path.stat()
            if (descriptor_metadata.st_dev, descriptor_metadata.st_ino) != (
                path_metadata.st_dev,
                path_metadata.st_ino,
            ):
                raise RuntimeError("inherited phase subject lock descriptors are unsafe")
        # Reassert the inherited lock modes on the same open file descriptions.
        # This is a no-op for a valid delegation, keeps working if the original
        # parent has already exited, and refuses separately opened descriptors
        # when another process owns the subject lock.
        fcntl.flock(activity_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        fcntl.flock(subject_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, ValueError) as exc:
        raise RuntimeError("cannot retain inherited phase subject locks") from exc
    return True


def claude_result_failure_reason(summary: dict[str, Any]) -> str | None:
    """Return why a terminal Claude stream summary is not a successful result."""
    if summary.get("parsed") is not True:
        return "missing final result event"
    result_event_found = summary.get("result_event_found")
    if result_event_found is False:
        return "missing final result event"
    if result_event_found is not True and "result_event_found" in summary:
        return "result_event_found must be a boolean"

    is_error = summary.get("is_error")
    if not isinstance(is_error, bool):
        return "final result is missing a boolean is_error flag"
    if is_error:
        api_error_status = summary.get("api_error_status")
        if isinstance(api_error_status, int) and not isinstance(api_error_status, bool):
            return f"final result reported an API error (status {api_error_status})"
        return "final result reported an error"

    result_subtype = summary.get("result_subtype")
    if result_subtype != "success":
        return f"final result subtype is {result_subtype!r}, expected 'success'"
    # Summaries written before result_event_found was introduced are safe to
    # accept here: the old parser could only populate these terminal fields
    # from an actual result event.
    return None


def validate_claude_session_reference(
    root: Path,
    claude_session_ref: Any,
    *,
    expected_phase: str,
    expected_subject: str,
    expected_outputs: list[str],
    report: ValidationReport | None = None,
) -> list[str]:
    """Validate that a manifest session points at the exact successful CC run."""
    errors: list[str] = []
    if not isinstance(claude_session_ref, str) or not claude_session_ref.strip():
        return ["claude_session_ref must be a non-empty string"]

    ref_path = Path(claude_session_ref)
    if ref_path.is_absolute():
        return ["claude_session_ref must be project-relative"]
    if any(part in {"", ".", ".."} for part in ref_path.parts):
        return ["claude_session_ref must be a normalized project-relative path"]

    try:
        root_resolved = root.resolve()
        expected_root = (
            root_resolved / "runs/claude-sessions" / expected_phase / expected_subject
        ).resolve()
        lexical_session_path = root_resolved / ref_path
        session_path = lexical_session_path.resolve()
    except (OSError, RuntimeError) as exc:
        return [f"cannot resolve claude_session_ref safely: {exc}"]
    if report is not None:
        report.checked_paths.append(str(session_path))
    try:
        expected_root.relative_to(root_resolved)
        session_path.relative_to(root_resolved)
        relative_session = session_path.relative_to(expected_root)
    except ValueError:
        return [
            "claude_session_ref must stay inside the project and under "
            f"runs/claude-sessions/{expected_phase}/{expected_subject}"
        ]
    if len(relative_session.parts) != 1 or relative_session.name in {"", ".", ".."}:
        errors.append("claude_session_ref must identify one run directory")
    if session_path != lexical_session_path:
        errors.append("claude_session_ref must not traverse symbolic links")
    if not session_path.is_dir():
        errors.append(f"claude_session_ref does not point to a directory: {claude_session_ref}")
        return errors

    status_path = session_path / "status.json"
    if report is not None:
        report.checked_paths.append(str(status_path))
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        errors.append(f"cannot read Claude session status {status_path}: {exc}")
        return errors
    except json.JSONDecodeError as exc:
        errors.append(f"invalid JSON in Claude session status {status_path}: {exc}")
        return errors
    if not isinstance(status, dict):
        errors.append(f"Claude session status must be a JSON object: {status_path}")
        return errors

    if status.get("phase") != expected_phase:
        errors.append(f"Claude session status phase must be {expected_phase!r}")
    if status.get("subject") != expected_subject:
        errors.append(f"Claude session status subject must be {expected_subject!r}")
    run_id = status.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        errors.append("Claude session status run_id must be a non-empty string")
    elif run_id != session_path.name:
        errors.append("Claude session status run_id must match its session directory")
    exit_code = status.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        errors.append("Claude session status exit_code must be an integer")
    elif exit_code != 0:
        errors.append("Claude session status exit_code must be 0")
    if status.get("timed_out") is not False:
        errors.append("Claude session status timed_out must be false")
    if status.get("cost_pending") is not False:
        errors.append("Claude session status cost_pending must be false")
    cost = status.get("cost")
    if not isinstance(cost, dict):
        errors.append("Claude session status cost must be an object")
    else:
        result_error = claude_result_failure_reason(cost)
        if result_error is not None:
            errors.append(f"Claude session result must be successful: {result_error}")
    result_validation_error = status.get("result_validation_error")
    if result_validation_error is not None:
        if not isinstance(result_validation_error, str):
            errors.append("Claude session status result_validation_error must be null or a string")
        elif result_validation_error:
            errors.append(
                "Claude session status contains a result validation error: "
                f"{result_validation_error}"
            )
    if "missing_outputs" in status:
        missing_outputs = status.get("missing_outputs")
        if not isinstance(missing_outputs, list) or any(
            not isinstance(item, str) for item in missing_outputs
        ):
            errors.append("Claude session status missing_outputs must be a list of strings")
        elif missing_outputs:
            errors.append("Claude session status missing_outputs must be empty")
    output_sync_error = status.get("output_sync_error")
    if output_sync_error is not None:
        errors.append("Claude session status output_sync_error must be null")
    synced_outputs = status.get("synced_outputs")
    if not isinstance(synced_outputs, list) or any(
        not isinstance(item, str) for item in synced_outputs
    ):
        errors.append("Claude session status synced_outputs must be a list of strings")
    elif synced_outputs != expected_outputs:
        errors.append(
            "Claude session status synced_outputs must exactly equal "
            f"{expected_outputs!r}, got {synced_outputs!r}"
        )
    return errors


def select_new_claude_session(
    root: Path,
    *,
    expected_phase: str,
    expected_subject: str,
    expected_outputs: list[str],
    before: set[Path],
) -> tuple[Path | None, list[str]]:
    session_root = root / "runs/claude-sessions" / expected_phase / expected_subject
    try:
        after = {path for path in session_root.iterdir() if path.is_dir()} if session_root.is_dir() else set()
    except OSError as exc:
        return None, [f"cannot inspect Claude session root {session_root}: {exc}"]
    created = sorted(after - before, key=lambda path: path.name)
    if not created:
        return None, ["no new Claude session directory was created"]

    valid: list[Path] = []
    errors: list[str] = []
    for session_path in created:
        try:
            session_ref = session_path.resolve().relative_to(root.resolve()).as_posix()
        except (OSError, ValueError, RuntimeError):
            errors.append(f"new Claude session is outside project root: {session_path}")
            continue
        candidate_errors = validate_claude_session_reference(
            root,
            session_ref,
            expected_phase=expected_phase,
            expected_subject=expected_subject,
            expected_outputs=expected_outputs,
        )
        if candidate_errors:
            errors.extend(f"{session_path}: {error}" for error in candidate_errors)
        else:
            valid.append(session_path)
    if len(valid) != 1:
        if len(valid) > 1:
            errors.append(f"multiple valid new Claude sessions were created: {[p.name for p in valid]}")
        elif not errors:
            errors.append("no valid new Claude session was created")
        return None, errors
    return valid[0], errors


def directory_tree_sha256(root: Path) -> str:
    """Hash path names, entry types, modes, symlink targets, and file bytes."""
    if not root.is_dir():
        raise OSError(f"directory does not exist: {root}")
    digest = hashlib.sha256()
    paths = [root, *sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())]
    for path in paths:
        rel = "." if path == root else path.relative_to(root).as_posix()
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if path.is_symlink():
            kind = "L"
        elif path.is_dir():
            kind = "D"
        elif path.is_file():
            kind = "F"
        else:
            kind = "O"
        digest.update(f"{kind}\0{rel}\0{mode:o}\0".encode("utf-8"))
        if kind == "L":
            digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
        elif kind == "F":
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


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

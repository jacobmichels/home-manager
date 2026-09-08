"""Conservative regular-file reconciliation; stdout carries a preflight plan.

The plan is kept in the activation shell, never in the home directory. Applications
must be quiescent during activation: POSIX has no compare-and-swap file replacement.
"""

import base64
import argparse
import difflib
from decimal import Decimal
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import shlex
import sys
import tempfile
import time

import tomlkit


class Divergence(Exception):
    pass


def resolution_guidance(old, new, name):
    baseline = Path(old) / "home-files" / name if old else None
    desired = Path(new) / "home-files" / name
    lines = [
        "",
        "To resolve:",
        "  Stop applications writing this file and back up the live file before editing.",
    ]
    if name in manifest(old) and baseline is not None and baseline.is_file():
        lines += [f"  Previous generated file: {baseline}"]
    else:
        lines += [
            "  No reconciled baseline is available; review live and desired contents for initial adoption."
        ]
    if desired.is_file():
        lines += [f"  New desired file: {desired}"]
    lines += [
        "  For content conflicts, choose one:",
        "    Keep local edits: undo the conflicting Nix change, or update the declaration to match the live contents.",
        "    Accept Nix: for an existing regular file, choose ONE:",
        "      Replace the entire file with declared contents:",
        f"        {shlex.quote(str(Path(new) / 'reconcile'))} --replace {shlex.quote(name)}",
        "      This command back up the live file and approve only this exact file state for the next activation.",
        "    Review a custom candidate without editing the live file:",
        f"        {shlex.quote(str(Path(new) / 'reconcile'))} --export {shlex.quote(name)}",
        "      Edit candidate in the printed workspace, then review and approve:",
        f"        {shlex.quote(str(Path(new) / 'reconcile'))} --accept WORKSPACE",
        "  Fix any file-type, ownership, or permission issue reported above first; matching contents does not bypass these checks.",
        "  Then rerun your usual Home Manager/NixOS activation command.",
        "  Failed activation does not advance the Home Manager baseline. Changing the live file or either generated version invalidates approval.",
    ]
    return "\n".join(lines)


def encode(data):
    return base64.b64encode(data).decode("ascii")


def manifest(generation):
    path = Path(generation) / "reconciliation.json"
    return json.loads(path.read_text()) if generation and path.exists() else []


def target(home, name):
    parts = name.split("/")
    if not name or any(p in ("", ".", "..") for p in parts):
        raise Divergence("target must be a normalized relative path")
    path = Path(home)
    for part in parts[:-1]:
        path /= part
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise Divergence("parent is a symlink or is not a directory")
    return path / parts[-1]


def snapshot(path):
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(before.st_mode):
        return {"link": os.readlink(path)}
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise Divergence("live target is not an individual regular file")
    if before.st_uid != os.geteuid() or before.st_gid != os.getegid():
        raise Divergence("live ownership differs from the activating user/group")
    if before.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
        raise Divergence("special permission bits are unsupported")
    try:
        attributes = os.listxattr(path)
    except OSError as error:
        if error.errno != errno.ENOTSUP:
            raise
        attributes = []
    if attributes:
        raise Divergence("extended attributes or ACLs require manual reconciliation")

    def identity(info):
        # Reading can update atime; it is not evidence of a concurrent edit.
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_nlink,
            info.st_uid,
            info.st_gid,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    with os.fdopen(
        os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb"
    ) as stream:
        if identity(os.fstat(stream.fileno())) != identity(before):
            raise Divergence("live file changed while being opened")
        data = stream.read()
    if identity(before) != identity(path.lstat()):
        raise Divergence("live file changed while being read")
    return {
        "data": encode(data),
        "mode": stat.S_IMODE(before.st_mode),
        "inode": before.st_ino,
        "device": before.st_dev,
        "mtime": before.st_mtime_ns,
        "ctime": before.st_ctime_ns,
    }


def validate_text(*contents):
    # Reject binary/non-UTF-8 inputs even when a byte comparison could succeed.
    for data in contents:
        if b"\0" in data:
            raise Divergence("binary files are unsupported")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise Divergence("non-UTF-8 files are unsupported") from error


def parse_json(data):
    validate_text(data)

    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Divergence("duplicate JSON object key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise Divergence("non-finite JSON number")

    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_float=Decimal,
            parse_constant=invalid_constant,
        )
    except (ValueError, ArithmeticError, RecursionError) as error:
        # Do not include configuration contents (potentially secrets) in errors.
        raise Divergence("invalid JSON") from error


def json_equal(left, right):
    # Python equates True with 1, including inside lists and dictionaries.
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(map(json_equal, left, right))
    return left == right


def merge_values(a, b, c, missing, equal, format_name, path=""):
    if equal(b, a):
        return c
    if equal(c, a) or equal(b, c):
        return b
    if isinstance(b, dict) and isinstance(c, dict) and (isinstance(a, dict) or a is missing):
        a = {} if a is missing else a
        result = {}
        for key in sorted(a.keys() | b.keys() | c.keys()):
            pointer = path + "/" + key.replace("~", "~0").replace("/", "~1")
            value = merge_values(
                a.get(key, missing), b.get(key, missing), c.get(key, missing),
                missing, equal, format_name, pointer,
            )
            if value is not missing:
                result[key] = value
        return result
    raise Divergence(f"conflicting {format_name} edits at JSON pointer {ascii(path)}")


def merge_json(base, live, desired):
    missing = object()
    a = missing if base is None else parse_json(base)
    b, c = map(parse_json, (live, desired))

    def render(value):
        # Decimal avoids rounding local numbers when another key changes.
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, dict):
            return (
                "{"
                + ", ".join(
                    json.dumps(key) + ": " + render(item) for key, item in value.items()
                )
                + "}"
            )
        if isinstance(value, list):
            return "[" + ", ".join(map(render, value)) + "]"
        return json.dumps(value)

    result = merge_values(a, b, c, missing, json_equal, "JSON")
    # Keep existing formatting when no combined document needs to be written.
    if json_equal(result, b):
        return live
    if json_equal(result, c):
        return desired
    return (render(result) + "\n").encode("utf-8")


def parse_toml(data):
    validate_text(data)
    try:
        return tomlkit.parse(data.decode("utf-8"))
    except (ValueError, ArithmeticError, RecursionError) as error:
        # Parser messages can contain configuration secrets.
        raise Divergence("invalid TOML") from error


def toml_equal(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            toml_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(map(toml_equal, left, right))
    if isinstance(left, float):
        if math.isnan(left) and math.isnan(right):
            return True
        if left == right == 0.0:
            return math.copysign(1.0, left) == math.copysign(1.0, right)
    return left == right


def merge_toml(base, live, desired):
    missing = object()
    a = missing if base is None else parse_toml(base).unwrap()
    live_doc, desired_doc = map(parse_toml, (live, desired))
    b, c = live_doc.unwrap(), desired_doc.unwrap()
    result = merge_values(a, b, c, missing, toml_equal, "TOML")
    if toml_equal(result, b):
        return live

    def update(document, before, after):
        for key in before.keys() - after.keys():
            del document[key]
        for key, value in after.items():
            if key in before and toml_equal(before[key], value):
                continue
            if key in before and isinstance(before[key], dict) and isinstance(value, dict):
                update(document[key], before[key], value)
            else:
                document[key] = value

    update(live_doc, b, result)
    encoded = tomlkit.dumps(live_doc).encode("utf-8")
    # Editing dotted keys and inline tables must still yield the intended data.
    if not toml_equal(parse_toml(encoded).unwrap(), result):
        raise Divergence("TOML serialization changed merged values")
    return encoded


def validate_format(name, data):
    if name.endswith(".json"):
        parse_json(data)
    elif name.endswith(".toml"):
        parse_toml(data)


def merge(base, live, desired, name=""):
    if name.endswith(".json"):
        return merge_json(base, live, desired)
    if name.endswith(".toml"):
        return merge_toml(base, live, desired)
    if base is None:
        validate_text(live, desired)
        if live == desired:
            return live
        raise Divergence(
            "text differs without a reconciled baseline; approve a resolution"
        )
    validate_text(base, live, desired)
    if live == base:
        return desired
    if desired == base or live == desired:
        return live
    a = base.splitlines(keepends=True)

    def edits(data):
        lines = data.splitlines(keepends=True)
        result = []
        for op, i, j, k, l in difflib.SequenceMatcher(
            None, a, lines, autojunk=False
        ).get_opcodes():
            if op == "equal":
                continue
            replacement = lines[k:l]
            result.append((i, j, replacement))
        return result

    live_lines = live.splitlines(keepends=True)
    left, right = edits(live), edits(desired)
    subsumed = []
    for i, j, replacement in left:
        for k, l, other in right:
            if (i, j, replacement) == (k, l, other):
                continue
            # Diff groups insertions adjacent to a replacement into one hunk.
            # Recognize an agreed single-line replacement inside such a hunk,
            # retaining the surrounding additions. Require a unique new line
            # and absence of the original line; otherwise alignment is ambiguous.
            if i == k and j == l and j == i + 1:
                smaller, larger = sorted((replacement, other), key=len)
                if (
                    len(smaller) == 1
                    and len(larger) > 1
                    and larger.count(smaller[0]) == 1
                    and a[i] not in larger
                ):
                    subsumed.append((i, j, smaller))
                    continue
            # A live insertion beside a uniquely retained baseline line can
            # stay on the same side when that line alone is replaced by Nix.
            # Do not extend this to deletions, multiline replacements, or
            # repeated anchors whose alignment could be arbitrary.
            if (
                i == j
                and l == k + 1
                and len(other) == 1
                and i in (k, l)
                and a.count(a[k]) == 1
                and live_lines.count(a[k]) == 1
                and other[0] not in live_lines
            ):
                continue
            # Adjacent replacements are independent. Other insertions at the
            # edge of another edit remain ambiguous and deliberately rejected.
            if (
                max(i, k) < min(j, l)
                or (i == j and k <= i <= l)
                or (k == l and i <= k <= j)
            ):
                raise Divergence("overlapping live and declarative edits")
    combined = left + [edit for edit in right if edit not in left]
    combined = [edit for edit in combined if edit not in subsumed]
    for i, j, replacement in sorted(combined, reverse=True):
        a[i:j] = replacement
    return b"".join(a)


def digest(data):
    return None if data is None else hashlib.sha256(data).hexdigest()


def approval_path(home, name):
    return target(
        home,
        ".local/state/home-manager/reconciliation/"
        + digest(os.fsencode(name))
        + ".json",
    )


STATE = ".local/state/home-manager/reconciliation/"


def write_record(path, record):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    existing = snapshot(path)
    if existing is not None and "data" not in existing:
        raise Divergence("cleanup record is not a regular file")
    fd, temporary = tempfile.mkstemp(prefix=".record-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_record(path):
    state = snapshot(path)
    if state is None or "data" not in state:
        raise Divergence(f"Missing regular cleanup record: {path}")
    return json.loads(base64.b64decode(state["data"]))


def workspace_snapshot(workspace):
    if workspace.is_symlink() or not workspace.is_dir():
        raise Divergence("workspace is not a directory")
    files = {}
    for path in workspace.iterdir():
        state = snapshot(path)
        if state is None or "data" not in state:
            raise Divergence("workspace contains a non-regular file")
        files[path.name] = state
    return files


def record_files(home, directory, pattern):
    root = target(home, (STATE + directory).rstrip("/"))
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise Divergence("cleanup state directory is not a directory")
    return sorted(root.glob(pattern))


def cleanup(home, dry_run=False, now=None):
    """Delete only recorded, unchanged artifacts; unknown files stay untouched."""
    now = time.time() if now is None else now
    # An unreadable approval must prevent pruning: it may protect any backup.
    approvals = [read_record(p) for p in record_files(home, "", "*.json")]
    protected = {approval.get("backup") for approval in approvals}
    pending_workspaces = {
        (approval.get("workspace") or {}).get("name") for approval in approvals
    }
    groups = {}
    for record_path in record_files(home, "backups", "*.json"):
        record = read_record(record_path)
        live = target(home, record["name"])
        backup = target(home, record["backup"])
        if backup.parent != live.parent or not backup.name.startswith(
            live.name + ".hm-backup-"
        ):
            raise Divergence("invalid recorded backup path")
        if snapshot(backup) is not None:
            groups.setdefault(record["name"], []).append((record_path, record, backup))
    for entries in groups.values():
        entries.sort(key=lambda item: (item[1]["created"], item[0].name), reverse=True)
        for index, (record_path, record, backup) in enumerate(entries):
            if (
                index < 3
                or now - record["created"] < 30 * 86400
                or str(backup) in protected
            ):
                continue
            if snapshot(backup) != record["snapshot"]:
                continue
            print(f"{'Would remove' if dry_run else 'Removing'} backup: {backup}")
            if not dry_run:
                backup.unlink()
                record_path.unlink()
    for metadata_path in record_files(home, "workspaces", "conflict-*.json"):
        metadata = read_record(metadata_path)
        resolved = metadata.get("resolved")
        if resolved is None or metadata_path.stem in pending_workspaces:
            continue
        workspace = target(home, STATE + "workspaces/" + metadata_path.stem)
        if workspace_snapshot(workspace) != resolved:
            continue
        print(
            f"{'Would remove' if dry_run else 'Removing'} resolved workspace: {workspace}"
        )
        if not dry_run:
            for name in resolved:
                (workspace / name).unlink()
            workspace.rmdir()
            metadata_path.unlink()


def finish_activation(home, plan):
    """Called only after every activation step and generation update succeeded."""
    try:
        for entry in plan:
            approval = entry.get("approval") or {}
            exported = approval.get("workspace")
            if not exported:
                continue
            live = snapshot(target(home, entry["name"]))
            if live is None or live.get("data") != entry["result"]:
                continue
            workspace = target(home, STATE + "workspaces/" + exported["name"])
            metadata_path = workspace.with_suffix(".json")
            if (
                snapshot(metadata_path) != exported["metadata"]
                or workspace_snapshot(workspace) != exported["files"]
            ):
                continue
            metadata = read_record(metadata_path)
            metadata["resolved"] = exported["files"]
            write_record(metadata_path, metadata)
        cleanup(home)
    except (Divergence, OSError, ValueError, KeyError, TypeError) as error:
        # Housekeeping must not turn an otherwise successful activation into failure.
        print(f"Reconciliation cleanup skipped: {error}", file=sys.stderr)


def approved_result(home, name, state, base, desired):
    receipt = snapshot(approval_path(home, name))
    if receipt is None:
        return None
    if "data" not in receipt:
        raise Divergence("resolution approval is not a regular file")
    approval = json.loads(base64.b64decode(receipt["data"]))
    if (
        approval.get("policy") == 2
        and approval.get("name") == name
        and approval.get("before") == state
        and approval.get("base") == digest(base)
        and approval.get("desired") == digest(desired)
    ):
        return approval
    return None


def resolution_inputs(home, old, new, filename):
    name = os.path.relpath(filename, home) if os.path.isabs(filename) else filename
    path = target(home, name)
    if name not in manifest(new):
        raise Divergence(
            "Explicit resolution requires a reconciled declaration in the desired generation."
        )
    state = snapshot(path)
    if state is None or "data" not in state or not state["mode"] & stat.S_IWUSR:
        raise Divergence(
            "Explicit resolution requires an owner-writable regular live file."
        )
    base_path = Path(old) / "home-files" / name if old else None
    desired_path = Path(new) / "home-files" / name
    has_base = name in manifest(old) and base_path is not None and base_path.is_file()
    if os.access(base_path if has_base else path, os.X_OK) != os.access(
        desired_path, os.X_OK
    ):
        raise Divergence(
            "Resolve executable-mode changes before accepting content changes."
        )
    base = base_path.read_bytes() if has_base else None
    desired = desired_path.read_bytes()
    live = base64.b64decode(state["data"])
    validate_text(base if base is not None else b"", live, desired)
    validate_format(name, desired)
    return name, state, base, live, desired


def resolve(home, old, new, filename, replace=True):
    """Back up and approve one exact A/B/C state; activation installs the result."""
    if not replace:
        raise Divergence(
            "Declared-preferred merging was removed; export and approve a reviewed candidate."
        )
    name, state, base, live, desired = resolution_inputs(home, old, new, filename)
    result = desired
    approve_candidate(home, name, state, base, live, desired, result)


def approve_candidate(home, name, state, base, live, desired, result, workspace=None):
    validate_format(name, result)
    path = target(home, name)
    receipt_path = approval_path(home, name)
    receipt_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Validate an existing receipt too; never follow an unexpected symlink.
    receipt_state = snapshot(receipt_path)
    if receipt_state is not None and "data" not in receipt_state:
        raise Divergence("resolution approval is not a regular file")
    if snapshot(path) != state:
        raise Divergence(
            "Live file changed while preparing resolution. Retry with the application stopped."
        )
    fd, backup = tempfile.mkstemp(prefix=path.name + ".hm-backup-", dir=path.parent)
    with os.fdopen(fd, "wb") as stream:
        stream.write(live)
        stream.flush()
        os.fsync(stream.fileno())
    approval = {
        "policy": 2,
        "name": name,
        "before": state,
        "base": digest(base),
        "desired": digest(desired),
        "result": encode(result),
        "backup": backup,
    }
    if workspace is not None:
        approval["workspace"] = workspace
    relative_backup = os.path.relpath(backup, home)
    write_record(
        target(
            home, STATE + "backups/" + digest(os.fsencode(relative_backup)) + ".json"
        ),
        {
            "name": name,
            "backup": relative_backup,
            "created": time.time(),
            "snapshot": snapshot(Path(backup)),
        },
    )
    fd, temporary = tempfile.mkstemp(prefix=".approval-", dir=receipt_path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(approval, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, receipt_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(
        f"Backup: {backup}\n"
        f"Approved candidate: {path}\n"
        "The live file has not been changed. Rerun activation to install the approved result."
    )


def export_conflict(home, old, new, filename):
    name, state, base, live, desired = resolution_inputs(home, old, new, filename)
    # Keep input snapshots outside the editable workspace. An agent editing the
    # candidate must not accidentally redefine the inputs being approved.
    root = target(home, ".local/state/home-manager/reconciliation/workspaces")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="conflict-", dir=root))
    metadata = {
        "name": name,
        "before": state,
        "base": digest(base),
        "desired": digest(desired),
        "old": str(Path(old).resolve()) if old else "",
        "new": str(Path(new).resolve()),
    }
    for filename, data in (
        ("base", base),
        ("live", live),
        ("declared", desired),
        ("candidate", live),
    ):
        if data is None:
            continue
        with (workspace / filename).open("xb") as stream:
            os.chmod(stream.fileno(), 0o600)
            stream.write(data)
    with workspace.with_suffix(".json").open("x") as stream:
        os.chmod(stream.fileno(), 0o600)
        json.dump(metadata, stream)
    print(
        f"Conflict workspace: {workspace}\n"
        "Edit candidate, then run reconcile --accept WORKSPACE to review and approve.\n"
        "These files may contain secrets. Nothing has been sent to an agent."
    )
    if base is None:
        print("No reconciled baseline is available; this workspace has no base file.")
    return workspace


def accept_conflict(home, old, new, directory, confirm=None):
    root = target(home, ".local/state/home-manager/reconciliation/workspaces")
    workspace = Path(os.path.abspath(directory))
    if workspace.parent != root or workspace.is_symlink() or not workspace.is_dir():
        raise Divergence(
            "Expected an exported conflict workspace, not a symlink or foreign directory."
        )
    metadata_state = snapshot(workspace.with_suffix(".json"))
    if metadata_state is None or "data" not in metadata_state:
        raise Divergence("Missing regular workspace metadata.")
    metadata = json.loads(base64.b64decode(metadata_state["data"]))
    inputs = resolution_inputs(home, old, new, metadata["name"])
    name, state, base, live, desired = inputs
    if (
        metadata["old"] != (str(Path(old).resolve()) if old else "")
        or metadata["new"] != str(Path(new).resolve())
        or metadata["before"] != state
        or metadata["base"] != digest(base)
        or metadata["desired"] != digest(desired)
    ):
        raise Divergence(
            "Conflict inputs changed. Export a fresh workspace and review again."
        )
    candidate_state = snapshot(workspace / "candidate")
    if candidate_state is None or "data" not in candidate_state:
        raise Divergence("Candidate must be a regular file.")
    candidate = base64.b64decode(candidate_state["data"])
    validate_text(candidate)
    validate_format(name, candidate)
    validation = (
        "JSON syntax validated"
        if name.endswith(".json")
        else "TOML syntax validated"
        if name.endswith(".toml")
        else "no format validation performed"
    )
    print(f"Review candidate for {target(home, name)} ({validation}):")
    # Escape controls so config contents cannot hide deletions with terminal
    # escape sequences. repr also makes absent final newlines visible.
    for line in difflib.unified_diff(
        live.decode().splitlines(keepends=True),
        candidate.decode().splitlines(keepends=True),
        fromfile="live",
        tofile="candidate",
    ):
        print(ascii(line))
    if candidate == live:
        print("No content differences.")
    answer = (confirm or input)("Approve this exact candidate? Type yes: ")
    if answer != "yes":
        print("Declined. No backup or approval created; live file unchanged.")
        return False
    if (
        snapshot(workspace / "candidate") != candidate_state
        or snapshot(workspace.with_suffix(".json")) != metadata_state
        or resolution_inputs(home, old, new, name) != inputs
    ):
        raise Divergence("Inputs or candidate changed during review. Review again.")
    files = workspace_snapshot(workspace)
    if files.get("candidate") != candidate_state:
        raise Divergence("Candidate changed while recording approval. Review again.")
    approve_candidate(
        home,
        name,
        state,
        base,
        live,
        desired,
        candidate,
        workspace={"name": workspace.name, "metadata": metadata_state, "files": files},
    )
    return True


def check(home, old, new):
    previous = set(manifest(old))
    current = set(manifest(new))
    plan = []
    errors = []
    for name in sorted(previous | current):
        path = name
        approval = None
        local_notice = None
        try:
            path = target(home, name)
            desired_path = Path(new) / "home-files" / name
            state = snapshot(path)
            if name not in current:
                if os.path.lexists(desired_path) and state is not None:
                    raise Divergence(
                        "move the reconciled file aside before enabling symlink management"
                    )
                # Declaration removal relinquishes ownership without deleting data.
                continue
            if not desired_path.is_file():
                raise Divergence("generated source is not a regular file")
            desired = desired_path.read_bytes()
            mode = 0o700 if os.access(desired_path, os.X_OK) else 0o600
            base_path = Path(old) / "home-files" / name if old else None
            if state is None:
                if name in previous:
                    raise Divergence("application deleted the managed file")
                result = merge(desired, desired, desired, name)
            elif "link" in state:
                expected = (
                    str((Path(old) / "home-files").resolve() / name) if old else None
                )
                if (
                    name in previous
                    or state["link"] != expected
                    or not base_path.is_file()
                ):
                    raise Divergence("unexpected live symlink")
                base = base_path.read_bytes()
                result = merge(base, base, desired, name)
            else:
                has_base = (
                    name in previous and base_path is not None and base_path.is_file()
                )
                if os.access(base_path if has_base else path, os.X_OK) != os.access(
                    desired_path, os.X_OK
                ):
                    raise Divergence(
                        "declarative executable-mode change requires manual reconciliation"
                    )
                mode = state["mode"]
                if not mode & stat.S_IWUSR:
                    raise Divergence("live file is not owner-writable")
                base = base_path.read_bytes() if has_base else None
                live = base64.b64decode(state["data"])
                approval = approved_result(home, name, state, base, desired)
                result = (
                    base64.b64decode(approval["result"])
                    if approval
                    else merge(base, live, desired, name)
                )
                if approval:
                    validate_format(name, desired)
                    validate_format(name, result)
                    print(
                        f"RESOLUTION APPROVED: {path}\nBackup: {approval['backup']}",
                        file=sys.stderr,
                    )
                if not approval and live != desired:
                    if result == live:
                        local_notice = {
                            "live": digest(live),
                            "declared": digest(desired),
                        }
                    elif live != base:
                        print(
                            f"MERGE READY: {path}\n"
                            "Live edits and declarative changes merge cleanly; result will be installed during activation.",
                            file=sys.stderr,
                        )
            plan.append(
                {
                    "name": name,
                    "before": state,
                    "result": encode(result),
                    "mode": mode,
                    "approval": approval,
                    "local_notice": local_notice,
                    "reconciler": str(Path(new) / "reconcile"),
                    "status_command": shlex.quote(str(Path(new) / "reconcile"))
                    + " --check --verbose",
                }
            )
        except (Divergence, OSError, ValueError) as error:
            errors.append(
                f"DIVERGENCE: {path}\n"
                f"File was not modified. {error}" + resolution_guidance(old, new, name)
            )
    if errors:
        raise Divergence("\n\n".join(errors))
    return plan


def report_local_changes(home, plan, verbose=False):
    """Notification cache only: never consulted when deciding file contents."""
    known = 0
    shown = 0
    for entry in plan:
        fingerprint = entry.get("local_notice")
        if fingerprint is None:
            continue
        try:
            cache = target(
                home,
                ".local/state/home-manager/reconciliation/notices/"
                + digest(os.fsencode(entry["name"]))
                + ".json",
            )
            cached = snapshot(cache)
            previous = (
                json.loads(base64.b64decode(cached["data"]))
                if cached and "data" in cached
                else None
            )
        except (Divergence, OSError, ValueError):
            previous = None
        if previous == fingerprint and not verbose:
            known += 1
            continue
        print(
            f"LOCAL CHANGES: {target(home, entry['name'])}\n"
            "Preserving live edits that differ from the declarative configuration.",
            file=sys.stderr,
            flush=True,
        )
        shown += 1
        if verbose:
            command = shlex.quote(entry["reconciler"])
            filename = shlex.quote(entry["name"])
            print(
                "  Keep local edits: no action required; the summary remains while contents differ.\n"
                "  Clear this notice by matching Nix to live: update the Nix declaration to generate these contents, then switch.\n"
                "  Or discard local edits and use the declared file (backs up and approves replacement):\n"
                f"    {command} --replace {filename}\n"
                "  To review a custom combination instead:\n"
                f"    {command} --export {filename}\n"
                f"    # Edit candidate in the printed workspace, then: {command} --accept WORKSPACE\n"
                "  After approval, run Home Manager activation to install it. If nix-swap skips Home Manager,\n"
                "  explicitly rerun its activation (on NixOS, restart your home-manager-USER.service).\n"
                "  Approval alone does not change the file or clear the notice. A custom candidate still\n"
                "  differing from the declaration will continue to appear in the local-change summary.\n"
                "  Applications may recreate local differences after writing their settings again.",
                file=sys.stderr,
            )
        # Persist only after printing, outside preflight. Cache failures must
        # never fail reconciliation or suppress the next notification.
        temporary = None
        try:
            cache = target(
                home,
                ".local/state/home-manager/reconciliation/notices/"
                + digest(os.fsencode(entry["name"]))
                + ".json",
            )
            cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            cached = snapshot(cache)
            if cached is not None and "data" not in cached:
                raise Divergence("notification cache is not a regular file")
            fd, temporary = tempfile.mkstemp(prefix=".notice-", dir=cache.parent)
            with os.fdopen(fd, "w") as stream:
                json.dump(fingerprint, stream)
            os.replace(temporary, cache)
        except (Divergence, OSError, ValueError) as error:
            print(f"Could not remember local-change notice: {error}", file=sys.stderr)
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)
    if known:
        print(
            f"Reconciliation: {known} file(s) have previously reported local changes.",
            file=sys.stderr,
        )
    if (known or shown) and not verbose:
        print(f"Show files: {plan[0]['status_command']}", file=sys.stderr)
        print(
            "Local changes are informational, not conflicts. The command above shows how to keep or resolve them.",
            file=sys.stderr,
        )


def apply(home, plan):
    # Validate the entire set again before the first write, including no-op files.
    for entry in plan:
        path = target(home, entry["name"])
        if snapshot(path) != entry["before"]:
            raise Divergence(
                f"DIVERGENCE: {path}\nFile was not modified. Changed after preflight.\n"
                "Stop applications writing this file, inspect the live contents, and retry activation."
            )
    for entry in plan:
        before = entry["before"]
        if before is not None and before.get("data") == entry["result"]:
            consume_approval(home, entry)
            continue
        path = target(home, entry["name"])
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".hm-reconcile-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(base64.b64decode(entry["result"]))
                stream.flush()
                os.fchmod(stream.fileno(), entry["mode"])
                os.fsync(stream.fileno())
            if snapshot(path) != before:
                raise Divergence(
                    f"DIVERGENCE: {path}\nFile was not modified. Changed after preflight.\n"
                    "Stop applications writing this file, inspect the live contents, and retry activation.\n"
                    "Earlier files may already have been installed."
                )
            os.replace(temporary, path)
            consume_approval(home, entry)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    report_local_changes(home, plan)


def consume_approval(home, entry):
    if entry.get("approval"):
        path = approval_path(home, entry["name"])
        receipt = snapshot(path)
        if receipt is not None and "data" in receipt:
            if json.loads(base64.b64decode(receipt["data"])) == entry["approval"]:
                path.unlink()


def report_status(home, old, new, verbose=False):
    """Inspect reconciliation without installing files or consuming approvals."""
    plan = check(home, old, new)
    report_local_changes(home, plan, verbose=verbose)
    pending = [entry for entry in plan if entry.get("approval")]
    updates = [
        entry
        for entry in plan
        if (entry["before"] or {}).get("data") != entry["result"]
    ]
    if pending:
        print(
            "Resolution is approved but has not been applied. Explicitly run Home Manager activation to apply it."
        )
    elif updates:
        print(
            "Reconciliation has pending file updates; run Home Manager activation to install them."
        )
    elif not any(
        entry["result"]
        != encode((Path(new) / "home-files" / entry["name"]).read_bytes())
        for entry in plan
    ):
        print("Reconciled files match the declaration.")
    print(
        "Check complete; no managed files were installed and no approvals were consumed. Notification history updated."
    )


if __name__ == "__main__":
    try:
        if sys.argv[1] == "check":
            print(json.dumps(check(*sys.argv[2:])))
        elif sys.argv[1] == "apply":
            apply(sys.argv[2], json.load(sys.stdin))
        elif sys.argv[1] == "finish":
            finish_activation(sys.argv[2], json.load(sys.stdin))
        elif sys.argv[1] == "list":
            for name in manifest(sys.argv[2]):
                sys.stdout.buffer.write(os.fsencode(name) + b"\0")
        elif sys.argv[1] == "changed":
            entry = next(e for e in json.load(sys.stdin) if e["name"] == sys.argv[2])
            sys.exit(0 if (entry["before"] or {}).get("data") != entry["result"] else 1)
        elif sys.argv[1] == "resolve":
            parser = argparse.ArgumentParser(
                description="Check reconciliation, or back up and approve one file for activation."
            )
            parser.add_argument("--generation", required=True)
            parser.add_argument(
                "--dry-run", action="store_true", help="preview --cleanup removals"
            )
            parser.add_argument(
                "--verbose",
                action="store_true",
                help="with --check, list all local changes",
            )
            choice = parser.add_mutually_exclusive_group(required=True)
            choice.add_argument(
                "--cleanup",
                action="store_true",
                help="prune old backups and resolved workspaces",
            )
            choice.add_argument("--replace", action="store_true")
            choice.add_argument(
                "--check",
                action="store_true",
                help="read-only check of all reconciled files",
            )
            choice.add_argument(
                "--export",
                action="store_true",
                help="export private A/B/C and candidate files",
            )
            choice.add_argument(
                "--accept",
                action="store_true",
                help="review and approve an exported workspace",
            )
            parser.add_argument(
                "file", nargs="?", help="absolute path, or path relative to HOME"
            )
            args = parser.parse_args(sys.argv[2:])
            if args.verbose and not args.check:
                parser.error("--verbose requires --check")
            if args.dry_run and not args.cleanup:
                parser.error("--dry-run requires --cleanup")
            if (args.check or args.cleanup) and args.file is not None:
                parser.error("--check and --cleanup do not accept a file argument")
            if not (args.check or args.cleanup) and args.file is None:
                parser.error("a file is required for resolution")
            home = os.environ["HOME"]
            state_home = Path(
                os.environ.get("XDG_STATE_HOME", str(Path(home) / ".local/state"))
            )
            old = state_home / "home-manager/gcroots/current-home"
            if args.cleanup:
                cleanup(home, dry_run=args.dry_run)
            elif args.check:
                report_status(
                    home,
                    str(old.resolve()) if old.exists() else "",
                    args.generation,
                    verbose=args.verbose,
                )
            elif args.export or args.accept:
                operation = export_conflict if args.export else accept_conflict
                operation(
                    home,
                    str(old.resolve()) if old.exists() else "",
                    args.generation,
                    args.file,
                )
            else:
                resolve(
                    home,
                    str(old.resolve()) if old.exists() else "",
                    args.generation,
                    args.file,
                    replace=args.replace,
                )
        else:
            raise ValueError("expected check or apply")
    except (Divergence, OSError, ValueError, KeyError, EOFError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)

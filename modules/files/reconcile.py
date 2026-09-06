"""Conservative regular-file reconciliation; stdout carries a preflight plan.

The plan is kept in the activation shell, never in the home directory. Applications
must be quiescent during activation: POSIX has no compare-and-swap file replacement.
"""

import base64
import difflib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile


class Divergence(Exception):
    pass


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
    if os.listxattr(path):
        raise Divergence("extended attributes or ACLs require manual reconciliation")
    def identity(info):
        # Reading can update atime; it is not evidence of a concurrent edit.
        return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                info.st_uid, info.st_gid, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns)

    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        if identity(os.fstat(stream.fileno())) != identity(before):
            raise Divergence("live file changed while being opened")
        data = stream.read()
    if identity(before) != identity(path.lstat()):
        raise Divergence("live file changed while being read")
    return {"data": encode(data), "mode": stat.S_IMODE(before.st_mode),
            "inode": before.st_ino, "device": before.st_dev,
            "mtime": before.st_mtime_ns, "ctime": before.st_ctime_ns}


def merge(base, live, desired):
    # Reject binary/non-UTF-8 inputs even when a byte comparison could succeed.
    for data in (base, live, desired):
        if b"\0" in data:
            raise Divergence("binary files are unsupported")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise Divergence("non-UTF-8 files are unsupported") from error
    if live == base:
        return desired
    if desired == base or live == desired:
        return live
    a = base.splitlines(keepends=True)

    def edits(data):
        lines = data.splitlines(keepends=True)
        return [(i, j, lines[k:l]) for op, i, j, k, l in
                difflib.SequenceMatcher(None, a, lines, autojunk=False).get_opcodes()
                if op != "equal"]

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
                if (len(smaller) == 1 and len(larger) > 1
                        and larger.count(smaller[0]) == 1 and a[i] not in larger):
                    subsumed.append((i, j, smaller))
                    continue
            # Adjacent replacements are independent. Insertions at the edge of
            # another edit are ambiguous and deliberately rejected.
            if max(i, k) < min(j, l) or (i == j and k <= i <= l) or (k == l and i <= k <= j):
                raise Divergence("overlapping live and declarative edits")
    combined = left + [edit for edit in right if edit not in left]
    combined = [edit for edit in combined if edit not in subsumed]
    for i, j, replacement in sorted(combined, reverse=True):
        a[i:j] = replacement
    return b"".join(a)


def check(home, old, new):
    previous = set(manifest(old))
    current = set(manifest(new))
    plan = []
    for name in sorted(previous | current):
        path = name
        try:
            path = target(home, name)
            desired_path = Path(new) / "home-files" / name
            state = snapshot(path)
            if name not in current:
                if os.path.lexists(desired_path) and state is not None:
                    raise Divergence("move the reconciled file aside before enabling symlink management")
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
                result = merge(b"", b"", desired)
            elif "link" in state:
                expected = str((Path(old) / "home-files").resolve() / name) if old else None
                if name in previous or state["link"] != expected or not base_path.is_file():
                    raise Divergence("unexpected live symlink")
                base = base_path.read_bytes()
                result = merge(base, base, desired)
            else:
                if name not in previous or not base_path.is_file():
                    raise Divergence("existing file has no reconciled baseline")
                if os.access(base_path, os.X_OK) != os.access(desired_path, os.X_OK):
                    raise Divergence("declarative executable-mode change requires manual reconciliation")
                mode = state["mode"]
                if not mode & stat.S_IWUSR:
                    raise Divergence("live file is not owner-writable")
                base = base_path.read_bytes()
                live = base64.b64decode(state["data"])
                result = merge(base, live, desired)
                if live != desired:
                    if result == live:
                        print(f"LOCAL CHANGES: {path}\n"
                              "Preserving live edits that differ from the declarative configuration.",
                              file=sys.stderr)
                    elif live != base:
                        print(f"MERGE READY: {path}\n"
                              "Live edits and declarative changes merge cleanly; result will be installed during activation.",
                              file=sys.stderr)
            plan.append({"name": name, "before": state, "result": encode(result), "mode": mode})
        except (Divergence, OSError) as error:
            raise Divergence(f"DIVERGENCE: {path}\n"
                             f"File was not modified. {error}") from error
    return plan


def apply(home, plan):
    # Validate the entire set again before the first write, including no-op files.
    for entry in plan:
        path = target(home, entry["name"])
        if snapshot(path) != entry["before"]:
            raise Divergence(f"DIVERGENCE: {path}\nFile was not modified. Changed after preflight.")
    for entry in plan:
        before = entry["before"]
        if before is not None and before.get("data") == entry["result"]:
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
                raise Divergence(f"DIVERGENCE: {path}\nFile was not modified. Changed after preflight.")
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


if __name__ == "__main__":
    try:
        if sys.argv[1] == "check":
            print(json.dumps(check(*sys.argv[2:])))
        elif sys.argv[1] == "apply":
            apply(sys.argv[2], json.load(sys.stdin))
        elif sys.argv[1] == "list":
            for name in manifest(sys.argv[2]):
                sys.stdout.buffer.write(os.fsencode(name) + b"\0")
        elif sys.argv[1] == "changed":
            entry = next(e for e in json.load(sys.stdin) if e["name"] == sys.argv[2])
            sys.exit(0 if (entry["before"] or {}).get("data") != entry["result"] else 1)
        else:
            raise ValueError("expected check or apply")
    except (Divergence, OSError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)

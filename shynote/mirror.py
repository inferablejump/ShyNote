"""Rebuild the local note tree and tracking from active upstream notes."""
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from .config import validate_notes_dir
from .model import Conflict, ShyNoteError, validate_path
from .working_copy import _atomic_write, _diff, _hash, _METADATA_PATHS


def _local_snapshot(root):
    """Read local contents without following links or crossing repository metadata."""
    files, directories = {}, {}
    if not root.exists():
        return files, directories

    def fail(error):
        raise error

    directories[""] = stat.S_IMODE(root.stat().st_mode)
    for directory, children, names in os.walk(root, onerror=fail, followlinks=False):
        for name in children + names:
            path = Path(directory) / name
            if name in _METADATA_PATHS:
                raise ShyNoteError(f"Mirror refuses to replace nested repository metadata: {path}")
            metadata = path.lstat()
            relative = path.relative_to(root).as_posix()
            if stat.S_ISDIR(metadata.st_mode):
                directories[relative] = stat.S_IMODE(metadata.st_mode)
            elif stat.S_ISREG(metadata.st_mode):
                files[relative] = (path.read_bytes(), stat.S_IMODE(metadata.st_mode))
            else:
                raise ShyNoteError(f"Mirror requires regular files and directories; unsupported local entry: {path}")
    return files, directories


def _inventory(store):
    paths, ids = {}, set()
    for note in store.list_notes():
        validate_path(note.path)
        if note.archived:
            raise Conflict("Upstream listing included an archived note; retry the mirror.")
        if note.path in paths:
            raise ShyNoteError(f"Cannot mirror duplicate upstream path {note.path!r}: {paths[note.path]} and {note.id}. Resolve the duplicate upstream first.")
        if note.id in ids:
            raise ShyNoteError(f"Cannot mirror duplicate upstream note ID {note.id}.")
        paths[note.path] = note.id
        ids.add(note.id)
    for name in paths:
        for parent in Path(name).parents:
            if parent.as_posix() in paths:
                raise ShyNoteError(f"Cannot mirror upstream path {str(parent)!r} as both a file and a directory.")
    return paths


def _preview(local_files, local_directories, notes, dry_run, show_diff):
    results = []
    for name in sorted(set(local_files) | set(notes)):
        before = local_files[name][0] if name in local_files else b""
        after = notes[name].body.encode("utf-8") if name in notes else b""
        if name in notes:
            status = "unchanged" if name in local_files and before == after else "would_pull" if dry_run else "pulled"
            result = {"file": name, "id": notes[name].id, "status": status}
        else:
            result = {"file": name, "status": "would_remove" if dry_run else "removed"}
        if show_diff:
            try:
                result["diff"] = _diff(before.decode("utf-8"), after.decode("utf-8"),
                                       name if name in local_files else "/dev/null",
                                       f"upstream/{name}" if name in notes else "/dev/null")
            except UnicodeError:
                result["diff"] = "Binary contents differ.\n" if before != after else ""
        results.append(result)
    desired_directories = {parent.as_posix() for name in notes for parent in Path(name).parents}
    for name in sorted(set(local_directories) - desired_directories - {""}):
        results.append({"file": name + "/", "status": "would_remove_directory" if dry_run else "removed_directory"})
    return results


def _install(copy, staged, backup, state, previous_state):
    """Roll back the directory and tracking if installation raises an exception."""
    # Keep state backup on its own filesystem so restoration can use atomic rename.
    state_directory = tempfile.mkdtemp(prefix="mirror-state-", dir=copy.directory)
    preserve_backup = False
    try:
        state_backup = Path(state_directory) / "state.json"
        if previous_state is not None:
            state_backup.write_bytes(previous_state)
        had_root, installed, moved_old = copy.root.exists(), False, False
        try:
            if had_root:
                os.replace(copy.root, backup)
                moved_old = True
            os.replace(staged, copy.root)
            installed = True
            _atomic_write(copy.state_path, json.dumps(state, indent=2) + "\n")
        except BaseException:
            try:
                if installed:
                    os.replace(copy.root, staged)
                if moved_old:
                    os.replace(backup, copy.root)
                if previous_state is not None:
                    os.replace(state_backup, copy.state_path)
                else:
                    copy.state_path.unlink(missing_ok=True)
            except BaseException as rollback_error:
                preserve_backup = True
                raise ShyNoteError(
                    f"Mirror installation and rollback failed. Recovery files retained at {backup.parent} "
                    f"and {state_directory}: {rollback_error}"
                ) from rollback_error
            raise
    finally:
        if not preserve_backup:
            shutil.rmtree(state_directory)


def mirror_upstream(copy, *, dry_run=False, show_diff=False):
    config = copy.notebook.config
    validate_notes_dir(copy.repo_root, config.notes_dir)
    if copy.root == copy.repo_root:
        raise ShyNoteError("pull --mirror requires a dedicated notes_dir; notes_dir = '.' would replace the project itself.")
    if copy.directory.is_symlink() or copy.state_path.is_symlink():
        raise ShyNoteError("Local tracking metadata must not be a symbolic link.")
    with copy._locked(dry_run):
        # Old tracking is deliberately not parsed: upstream rebuilds it from scratch.
        previous_state = copy.state_path.read_bytes() if copy.state_path.exists() else None
        local_files, local_directories = _local_snapshot(copy.root)
        store = copy.notebook.store
        inventory = _inventory(store)
        notes = {}
        state = {"version": 1, "identity": copy.identity, "files": {}}
        for name, note_id in sorted(inventory.items()):
            note = store.read(note_id)
            if note.id != note_id or note.path != name or note.archived:
                raise Conflict(f"Upstream note {note_id} changed identity, path, or archive state during mirror; retry.")
            notes[name] = note
            state["files"][name] = {"id": note.id, "revision": note.revision,
                                    "local_hash": _hash(note.body), "remote_hash": _hash(note.body)}
        if _inventory(store) != inventory:
            raise Conflict("Upstream note membership changed during mirror; retry. Local notes were not replaced.")
        results = _preview(local_files, local_directories, notes, dry_run, show_diff)
        if dry_run:
            return {"operation": "pull", "mirror": True, "dry_run": True, "results": results}
        copy.root.parent.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix=".shynote-mirror-", dir=copy.root.parent))
        installed = False
        try:
            staged = Path(directory) / "notes"
            staged.mkdir(mode=local_directories.get("", 0o700))
            for name, note in notes.items():
                path = staged / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(note.body.encode("utf-8"))
                path.chmod(local_files[name][1] if name in local_files else 0o600)
            # Revalidate after network/staging work, before discarding local drafts.
            validate_notes_dir(copy.repo_root, config.notes_dir)
            if _local_snapshot(copy.root) != (local_files, local_directories):
                raise Conflict("Local notes changed during mirror; retry. Local notes were not replaced.")
            if copy.state_path.is_symlink():
                raise Conflict("Local tracking changed during mirror; retry.")
            current_state = copy.state_path.read_bytes() if copy.state_path.exists() else None
            if current_state != previous_state:
                raise Conflict("Local tracking changed during mirror; retry.")
            _install(copy, staged, Path(directory) / "previous-notes", state, previous_state)
            installed = True
        finally:
            # Never delete the original tree if rollback could not restore it.
            if installed or not (directory / "previous-notes").exists():
                shutil.rmtree(directory)
        return {"operation": "pull", "mirror": True, "dry_run": False, "results": results}

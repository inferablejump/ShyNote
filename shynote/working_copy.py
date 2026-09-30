"""Explicit, per-file transfer between a local working copy and one notebook."""
from contextlib import contextmanager, nullcontext
from difflib import unified_diff
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .model import Conflict, CreatedNoteError, ShyNoteError, UnsupportedCapability, validate_path, validate_title

_METADATA_PATHS = {".git", ".shynote", ".shynote-local"}


def _hash(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _diff(before, after, before_name, after_name):
    # Preserve the distinction between files with/without a final newline.
    lines = unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                         fromfile=before_name, tofile=after_name)
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                   for line in lines)


def _read(path):
    return path.read_bytes().decode("utf-8")


def _atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, temporary = tempfile.mkstemp(prefix=".shynote-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class WorkingCopy:
    def __init__(self, notebook):
        self.notebook = notebook
        self.repo_root = notebook.config.root.resolve()
        self.root = self.repo_root / notebook.config.notes_dir
        self.directory = self.repo_root / ".shynote-local"
        self.state_path = self.directory / "state.json"
        config = notebook.config
        settings = config.storage
        self.identity = {"backend": config.backend, "notes_dir": config.notes_dir}
        if config.backend == "s3":
            self.identity.update(bucket=settings.bucket, prefix=settings.prefix,
                                 notebook=config.notebook, endpoint_url=settings.endpoint_url)
        else:
            self.identity.update(parent_page_id=settings.parent_page_id)

    def _path(self, name):
        validate_path(name)
        relative = Path(name)
        path = self.root
        for part in relative.parts:
            path = path / part
            if path.is_symlink():
                raise ShyNoteError("Note paths must not contain symbolic links.")
        return path

    def _load(self):
        if self.directory.is_symlink() or self.state_path.is_symlink():
            raise ShyNoteError("Local tracking metadata must not be a symbolic link.")
        if not self.state_path.exists():
            return {"version": 1, "identity": self.identity, "files": {}}
        try:
            state = json.loads(_read(self.state_path))
            if state["version"] != 1 or not isinstance(state["files"], dict):
                raise ValueError("unsupported state format")
            if state["identity"] != self.identity:
                raise ShyNoteError("Local tracking belongs to a different notebook or notes directory. Use a separate working copy.")
            ids = set()
            for name, entry in state["files"].items():
                self._path(name)
                if Path(name).as_posix() != name:
                    raise ValueError("noncanonical file path")
                if not all(isinstance(entry[key], str) and entry[key]
                           for key in ("id", "revision", "local_hash", "remote_hash")):
                    raise ValueError("invalid tracking entry")
                if not all(re.fullmatch(r"[0-9a-f]{64}", entry[key]) for key in ("local_hash", "remote_hash")):
                    raise ValueError("invalid content hash")
                if entry["id"] in ids:
                    raise ValueError("a remote note is tracked by more than one file")
                ids.add(entry["id"])
            return state
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise ShyNoteError("Invalid local tracking state; refusing to reset it or overwrite notes.") from exc

    @contextmanager
    def _locked(self, dry_run):
        # Dry runs create neither tracking metadata nor lock files.
        if dry_run:
            yield
            return
        if self.directory.is_symlink():
            raise ShyNoteError("Local tracking metadata must not be a symbolic link.")
        self.directory.mkdir(exist_ok=True)
        lock = self.directory / "lock"
        try:
            lock.mkdir()
        except FileExistsError as exc:
            raise ShyNoteError("Another transfer is running. If it crashed, remove .shynote-local/lock after confirming it stopped.") from exc
        try:
            yield
        finally:
            lock.rmdir()

    def _markdown_paths(self):
        """Discover local notes without traversing metadata or symbolic links."""
        if not self.root.exists():
            return

        def fail(error):
            raise error

        for directory, children, files in os.walk(self.root, onerror=fail, followlinks=False):
            parent = Path(directory)
            children[:] = [name for name in children
                           if name not in _METADATA_PATHS and not (parent / name).is_symlink()]
            for name in files:
                path = parent / name
                if path.suffix.lower() == ".md" and not path.is_symlink() and path.is_file():
                    yield path.relative_to(self.root).as_posix()

    def transfer(self, operation, file=None, *, all_files=False, dry_run=False,
                 show_diff=False, title=None, note_id=None, unconditional=False, mirror=False):
        if operation not in {"push", "pull"}:
            raise ShyNoteError("Transfer must be push or pull.")
        requested = [] if file is None else [file] if isinstance(file, (str, Path)) else list(file)
        if mirror:
            if operation != "pull" or requested or all_files or title is not None or note_id is not None or unconditional:
                raise ShyNoteError("Use pull --mirror without FILE, --all, --id, --title, or --unconditional.")
            from .mirror import mirror_upstream
            return mirror_upstream(self, dry_run=dry_run, show_diff=show_diff)
        restore_path = operation == "pull" and note_id and not requested and not all_files
        if bool(requested) == all_files and not restore_path:
            raise ShyNoteError("Choose FILE arguments, --all, or pull --id NOTE_ID to use its saved path.")
        if (all_files or len(requested) > 1) and (title is not None or note_id is not None):
            raise ShyNoteError("--title and --id apply to a single file only.")
        if (operation == "pull" and title is not None) or (operation == "push" and note_id is not None):
            raise ShyNoteError("Use --title for a first push, or --id for a first pull.")
        # Backends may scope request caches to one transfer; S3 needs no batch hook.
        batch = getattr(self.notebook.store, "transfer_batch", nullcontext)
        with self._locked(dry_run), batch():
            state = self._load()
            if restore_path:
                remote = self.notebook.store.read(note_id)
                validate_path(remote.path)
                requested = [remote.path]
            if all_files:
                names = set(state["files"])
                if operation == "push":
                    names.update(self._markdown_paths())
                names = sorted(names)
            else:
                names = list(dict.fromkeys(Path(name).as_posix() for name in requested))
            results = []
            for name in names:
                result = {"file": name}
                entry = state["files"].get(name)
                if entry:
                    result["id"] = entry["id"]
                try:
                    path = self._path(name)
                    if all_files and not path.exists():
                        result["status"] = "skipped_missing"
                    else:
                        self._transfer_one(state, result, path, operation, entry, dry_run,
                                           show_diff, title, note_id, unconditional)
                except (ShyNoteError, OSError, UnicodeError) as exc:
                    result.update(status="conflict" if isinstance(exc, Conflict) else "error", error=str(exc))
                    if isinstance(exc, CreatedNoteError):
                        result["id"] = exc.note_id
                results.append(result)
                if result["status"] in {"error", "conflict"}:
                    break
            return {"operation": operation, "dry_run": dry_run, "results": results}

    def _record(self, state, name, local_body, remote):
        state["files"][name] = {"id": remote.id, "revision": remote.revision,
                                "local_hash": _hash(local_body), "remote_hash": _hash(remote.body)}
        _atomic_write(self.state_path, json.dumps(state, indent=2) + "\n")

    def _unchanged_locally(self, path, original):
        self._path(path.relative_to(self.root).as_posix())
        current = _read(path) if path.exists() else None
        if current != original:
            raise Conflict("The local file changed during the transfer; retry after reviewing it.")

    def _transfer_one(self, state, result, path, operation, entry, dry_run,
                      show_diff, title, note_id, unconditional):
        store = self.notebook.store
        name = result["file"]
        local = _read(path) if path.exists() else None
        if entry is None and operation == "push":
            if local is None:
                raise ShyNoteError(f"Local note file does not exist: {path}")
            title = path.stem if title is None else title
            validate_title(title)
            if show_diff:
                result["diff"] = _diff("", local, "/dev/null", f"remote/{name}")
            result.update(status="would_create" if dry_run else "created", title=title)
            if not dry_run:
                self._unchanged_locally(path, local)
                remote = store.create(title, local, path=name)
                result["id"] = remote.id
                if remote.path != name:
                    raise Conflict("The created note has a different remote path; inspect it before retrying.")
                self._record(state, name, local, remote)
            return
        if entry is None and not note_id:
            raise ShyNoteError(f"File is not tracked: {path}. Use pull FILE --id NOTE_ID to fetch a remote note.")
        if entry and title is not None:
            raise ShyNoteError("--title is only supported on the first push of a file.")
        if entry and note_id and note_id != entry["id"]:
            raise ShyNoteError("This file already tracks a different remote note.")
        remote = store.read(entry["id"] if entry else note_id)
        result["id"] = remote.id
        if remote.path != name:
            raise ShyNoteError(f"Remote note path is {remote.path!r}, but the requested local path is {name!r}. Use pull --id {remote.id} to restore its saved path.")
        if any(other != name and tracked["id"] == remote.id for other, tracked in state["files"].items()):
            raise ShyNoteError("This remote note is already tracked by another local file.")
        if show_diff:
            before, after = (remote.body, local or "") if operation == "push" else (local or "", remote.body)
            before_name, after_name = (f"remote/{name}", name) if operation == "push" else (name, f"remote/{name}")
            result["diff"] = _diff(before, after, before_name, after_name)
        if entry is None:
            if local is not None and local != remote.body:
                raise Conflict(f"An untracked local file already exists: {path}. Choose a new path or reconcile it first.")
            result["status"] = "would_pull" if dry_run else "pulled"
            if not dry_run:
                self._unchanged_locally(path, local)
                if local is None:
                    # Exclusive creation also protects a new file appearing after the check.
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("xb") as stream:
                        stream.write(remote.body.encode("utf-8"))
                self._record(state, name, remote.body, remote)
            return
        if local is None:
            raise ShyNoteError(f"Tracked file is missing: {path}. Local deletion never deletes the remote note.")
        local_changed = _hash(local) != entry["local_hash"]
        remote_changed = _hash(remote.body) != entry["remote_hash"]
        if local == remote.body or (not local_changed and not remote_changed):
            result["status"] = "unchanged"
            if show_diff:
                result["diff"] = ""
            if not dry_run:
                self._record(state, name, local, remote)
            return
        if local_changed and remote_changed:
            raise Conflict("Both local and remote content changed. Reconcile them before retrying; neither was overwritten.")
        if operation == "pull" and local_changed:
            result["status"] = "local_changes"
            return
        if operation == "push" and remote_changed:
            result["status"] = "remote_changes"
            return
        if operation == "push":
            if remote.archived:
                raise Conflict("The remote note is archived; it cannot be updated.")
            if not store.capabilities.conditional_writes and not unconditional:
                raise UnsupportedCapability("This backend requires push --unconditional; concurrent remote edits cannot be protected atomically.")
            result["status"] = "would_push" if dry_run else "pushed"
            if not dry_run:
                self._unchanged_locally(path, local)
                guard = {"revision": remote.revision} if store.capabilities.conditional_writes else {"unconditional": True}
                acknowledged_body = store.update(remote.id, local, **guard)
                written = store.read(remote.id)
                if written.body != acknowledged_body or written.path != name:
                    raise Conflict("The write completed, but remote content differs from the acknowledged write on readback. Tracking was not advanced; inspect the remote note before retrying.")
                self._record(state, name, local, written)
        else:
            result["status"] = "would_pull" if dry_run else "pulled"
            if not dry_run:
                self._unchanged_locally(path, local)
                _atomic_write(path, remote.body)
                self._record(state, name, remote.body, remote)

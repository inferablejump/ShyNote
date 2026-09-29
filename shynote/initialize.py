"""Interactive and non-interactive setup share validation and application logic."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from .config import load_config, parse_config, validate_notes_dir
from .model import ConfigurationError, ShyNoteError
from .notebook import notebook_from_config
from .working_copy import _atomic_write


def add_arguments(parser):
    parser.add_argument("--repo", dest="init_repo", type=Path, help="Explicit directory to initialize")
    parser.add_argument("--non-interactive", action="store_true", help="Never prompt; report missing arguments")
    parser.add_argument("--backend", choices=("aws", "s3", "notion"))
    parser.add_argument("--notes-dir", help="Local notes directory (default: .agent/notes)")
    parser.add_argument("--notebook", help="Notebook identifier (default: generated UUID)")
    parser.add_argument("--bucket", help="Existing S3 bucket")
    parser.add_argument("--prefix", help="S3 prefix (default: shynote)")
    parser.add_argument("--region", help="AWS region")
    parser.add_argument("--endpoint-url", help="Optional S3-compatible endpoint")
    parser.add_argument("--parent-page", help="Existing Notion parent page URL or UUID")
    parser.add_argument("--token-env", help="Notion token environment variable (default: NOTION_TOKEN)")
    parser.add_argument("--no-ignore-notes", action="store_false", dest="ignore_notes", default=None,
                        help="Do not add the notes directory to .gitignore")


def _git(root, *arguments):
    try:
        return subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, timeout=10)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired as exc:
        raise ConfigurationError("Git repository discovery timed out.") from exc


def _root(explicit):
    start = Path(explicit).resolve() if explicit is not None else Path.cwd().resolve()
    if not start.is_dir():
        raise ConfigurationError("Repository path must be an existing directory.")
    # Match normal notebook discovery, including an existing marker above cwd.
    for directory in (start, *start.parents):
        marker = directory / ".shynote"
        if marker.exists() or marker.is_symlink():
            return directory
    if explicit is None:
        result = _git(start, "rev-parse", "--show-toplevel")
        if result is not None and result.returncode == 0:
            return Path(os.fsdecode(result.stdout).strip()).resolve()
    return start


def _ask(label, default=None):
    suffix = f" [{default}]" if default is not None else ""
    while True:
        print(f"{label}{suffix}: ", end="", file=sys.stderr, flush=True)
        try:
            answer = input().strip()
        except EOFError as exc:
            raise ConfigurationError("Initialization cancelled: input closed.") from exc
        if answer:
            return answer
        if default is not None:
            return default
        print("A value is required.", file=sys.stderr)


def _yes(label, default=True):
    while True:
        answer = _ask(label, "yes" if default else "no").casefold()
        if answer in {"yes", "y", "no", "n"}:
            return answer in {"yes", "y"}
        print("Enter yes or no.", file=sys.stderr)


def _parent_id(value):
    try:
        return str(UUID(value))
    except ValueError:
        try:
            url = urlsplit(value)
        except ValueError as exc:
            raise ConfigurationError("--parent-page must be a Notion page URL or UUID.") from exc
        if (url.scheme != "https" or url.username or url.password or not url.hostname
                or not any(url.hostname == domain or url.hostname.endswith("." + domain)
                           for domain in ("notion.com", "notion.so", "notion.site"))):
            raise ConfigurationError("--parent-page must be a Notion page URL or UUID.")
        # Page IDs occur at the end of the URL path; ignore query strings and view IDs.
        match = re.search(r"([0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})/?$", url.path)
        if not match:
            raise ConfigurationError("Cannot find a page UUID in --parent-page.")
        return str(UUID(match.group(1)))


def _missing(args):
    if args.backend is None:
        return ["--backend", "--bucket and --region (S3), or --parent-page (Notion)"]
    required = ("bucket", "region") if args.backend in {"aws", "s3"} else ("parent_page",)
    return ["--" + name.replace("_", "-") for name in required if not getattr(args, name)]


def _marker(config):
    data = {"version": 1, "notebook": config.notebook, "backend": config.backend,
            "notes_dir": config.notes_dir}
    lines = [f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in data.items()]
    lines.extend(["", "[storage]"])
    lines.extend(f"{key} = {json.dumps(value, ensure_ascii=False)}"
                 for key, value in asdict(config.storage).items() if value is not None)
    return "\n".join(lines) + "\n"


def _ignore_pattern(directory):
    # Escape Git's glob syntax so a configured path is treated literally.
    escaped = "".join("\\" + char if char in "\\*?[]!# " else char for char in directory)
    return f"/{escaped}/"


def _ignore_plan(root, notes_dir, ignore_notes):
    path = root / ".gitignore"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ConfigurationError(".gitignore must be a regular file, not a symbolic link or directory.")
    before = path.read_bytes().decode("utf-8") if path.exists() else None
    content = before or ""
    entries = ["/.shynote-local/"]
    if ignore_notes:
        if notes_dir == ".":
            raise ConfigurationError("Cannot ignore the whole repository; choose a notes subdirectory or --no-ignore-notes.")
        entries.append(_ignore_pattern(notes_dir))
    added = [entry for entry in entries if entry not in content.splitlines()]
    if added:
        content += ("\n" if content and not content.endswith("\n") else "") + "\n".join(added) + "\n"
    return before, content, added


def _check_local_writes(root, notes):
    # Probe actual directory write access without leaving any setup files behind.
    ancestor = notes
    while not ancestor.exists():
        ancestor = ancestor.parent
    for directory in {root, ancestor}:
        with tempfile.TemporaryFile(dir=directory):
            pass


def _apply(config, marker, ignore_before, ignore_after):
    root = config.root
    notes = root / config.notes_dir
    ignore = root / ".gitignore"
    marker_path = root / ".shynote"
    validate_notes_dir(root, config.notes_dir)
    if marker_path.exists() or marker_path.is_symlink():
        raise ConfigurationError("A .shynote marker appeared during setup; refusing to overwrite it.")
    if ignore.is_symlink() or (ignore.read_bytes().decode("utf-8") if ignore.exists() else None) != ignore_before:
        raise ConfigurationError(".gitignore changed during setup; run init again.")
    missing = []
    current = notes
    while not current.exists():
        missing.append(current)
        current = current.parent
    created = []
    ignore_written = False
    marker_created = False
    try:
        for directory in reversed(missing):
            directory.mkdir()
            created.append(directory)
        if ignore_after != ignore_before:
            _atomic_write(ignore, ignore_after)
            ignore_written = True
        # Exclusive creation protects existing configuration, including a concurrent init.
        with marker_path.open("x", encoding="utf-8") as stream:
            marker_created = True
            stream.write(marker)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if marker_created:
            marker_path.unlink()
        if ignore_written and ignore.read_bytes().decode("utf-8") == ignore_after:
            if ignore_before is None:
                ignore.unlink()
            else:
                _atomic_write(ignore, ignore_before)
        for directory in reversed(created):
            try:
                directory.rmdir()
            except OSError:
                pass  # Preserve files another process may have placed here.
        raise


def initialize(args):
    if args.repo is not None and args.init_repo is not None:
        raise ConfigurationError("Specify --repo only once.")
    root = _root(args.init_repo if args.init_repo is not None else args.repo)
    if (root / ".shynote").exists() or (root / ".shynote").is_symlink():
        config = load_config(root)
        return {"status": "already_initialized", "root": str(root), "backend": config.backend,
                "notebook": config.notebook, "notes_dir": config.notes_dir}
    missing = _missing(args)
    wizard = bool(missing) and not args.non_interactive and sys.stdin.isatty()
    if missing and not wizard:
        raise ConfigurationError("Missing initialization arguments: " + ", ".join(missing) + ".")
    if wizard:
        print(f"Repository: {root}", file=sys.stderr)
        if args.notes_dir is None:
            args.notes_dir = _ask("Notes directory", ".agent/notes")
        if args.backend is None:
            while args.backend not in {"aws", "s3", "notion"}:
                args.backend = _ask("Backend (aws/s3 or notion)").casefold()
        if args.backend in {"aws", "s3"}:
            args.bucket = args.bucket or _ask("Existing S3 bucket")
            args.region = args.region or _ask("AWS region", os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
            if args.prefix is None:
                args.prefix = _ask("S3 prefix", "shynote")
        else:
            args.parent_page = args.parent_page or _ask("Notion parent page URL or UUID")
        if args.ignore_notes is None:
            args.ignore_notes = _yes("Ignore the local notes directory in Git?")
    backend = "s3" if args.backend == "aws" else args.backend
    if backend == "s3":
        if args.parent_page is not None or args.token_env is not None:
            raise ConfigurationError("--parent-page and --token-env are only for Notion.")
        storage = {"bucket": args.bucket, "region": args.region, "prefix": args.prefix if args.prefix is not None else "shynote"}
        if args.endpoint_url is not None:
            storage["endpoint_url"] = args.endpoint_url
    else:
        if any(value is not None for value in (args.bucket, args.region, args.prefix, args.endpoint_url)):
            raise ConfigurationError("S3 storage arguments cannot be used with Notion.")
        storage = {"parent_page_id": _parent_id(args.parent_page),
                   "token_env": args.token_env if args.token_env is not None else "NOTION_TOKEN"}
    config = parse_config({"version": 1, "backend": backend, "storage": storage,
                           "notebook": args.notebook if args.notebook is not None else str(uuid4()),
                           "notes_dir": args.notes_dir if args.notes_dir is not None else ".agent/notes"}, root)
    ignore_notes = args.ignore_notes is not False
    before, after, additions = _ignore_plan(root, config.notes_dir, ignore_notes)
    _check_local_writes(root, root / config.notes_dir)
    warnings = []
    if ignore_notes:
        tracked = _git(root, "ls-files", "-z", "--", config.notes_dir)
        if tracked is not None and tracked.returncode == 0 and tracked.stdout:
            warnings.append("The notes directory contains Git-tracked files. Ignore rules do not untrack them; existing tracking is unchanged.")
    try:
        notebook_from_config(config).store.check_access()
    except ShyNoteError as exc:
        raise ConfigurationError(f"Cannot verify {backend} read access: {exc}") from exc
    marker = _marker(config)
    if wizard:
        print(f"Read access verified; write access has not been tested.\n\n{root / '.shynote'}:\n{marker}", file=sys.stderr)
        print(f"Notes directory: {root / config.notes_dir}\n.gitignore additions: {', '.join(additions) or '(none)'}", file=sys.stderr)
        for warning in warnings:
            print(warning, file=sys.stderr)
        if not _yes("Apply this setup?"):
            return {"status": "cancelled", "root": str(root)}
    _apply(config, marker, before, after)
    return {"status": "initialized", "root": str(root), "notes_dir": config.notes_dir,
            "notes_root": str(root / config.notes_dir), "notebook": config.notebook,
            "backend": config.backend, "storage": asdict(config.storage),
            "read_access": "verified", "write_access": "not_tested",
            "gitignore_added": additions, "warnings": warnings}

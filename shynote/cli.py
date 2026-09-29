import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from .model import ShyNoteError
from .notebook import open_notebook
from .working_copy import WorkingCopy


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="shynote")
    parser.add_argument("--repo", help="Repository directory (default: discover from current directory)")
    commands = parser.add_subparsers(dest="command", required=True)
    from .initialize import add_arguments, initialize
    add_arguments(commands.add_parser("init", help="Configure a notebook, prompting for missing settings in a terminal"))
    commands.add_parser("info", help="Inspect configuration and capabilities without network access")
    commands.add_parser("list", help="List active note titles without downloading note bodies")
    read = commands.add_parser("read")
    read.add_argument("id")
    for name in ("push", "pull"):
        scope = "local Markdown and tracked files" if name == "push" else "locally tracked files"
        transfer = commands.add_parser(name, help=f"{name.capitalize()} selected files or all {scope}")
        transfer.add_argument("files", nargs="*", type=Path, metavar="FILE", help="File paths relative to the configured notes directory")
        transfer.add_argument("--all", action="store_true", dest="all_files", help=f"Select all {scope} under the notes directory")
        transfer.add_argument("--dry-run", action="store_true", help="Preview diffs without changing files or remote notes")
        transfer.add_argument("--diff", action="store_true", dest="show_diff", help="Include unified diffs in JSON output")
        if name == "push":
            transfer.add_argument("--title", help="Title for a new note (default: file stem)")
            transfer.add_argument("--unconditional", action="store_true", help="Allow Notion writes without atomic conflict protection")
        else:
            transfer.add_argument("--id", dest="note_id", help="Remote note ID for the first pull into an untracked file")
    archive = commands.add_parser("archive")
    archive.add_argument("id")
    guard = archive.add_mutually_exclusive_group(required=True)
    guard.add_argument("--revision")
    guard.add_argument("--unconditional", action="store_true")
    for name, target in (("search-title", "titles"), ("search-content", "content")):
        search = commands.add_parser(name, help=f"Search note {target} if supported by the backend")
        search.add_argument("query")
    args = parser.parse_args(argv)
    if args.command in {"push", "pull"}:
        if bool(args.files) == args.all_files:
            parser.error("Choose one or more FILE arguments, or --all.")
        if (args.all_files or len(args.files) != 1) and (
                getattr(args, "title", None) is not None or getattr(args, "note_id", None) is not None):
            parser.error("--title and --id apply to a single file only.")
    try:
        if args.command == "init":
            print(json.dumps(initialize(args), ensure_ascii=False))
            return 0
        notebook = open_notebook(args.repo or ".")
        store = notebook.store
        if args.command == "info":
            result = {"notebook": notebook.config.notebook, "backend": notebook.config.backend,
                      "root": str(notebook.config.root), "notes_dir": notebook.config.notes_dir,
                      "capabilities": asdict(store.capabilities)}
        elif args.command == "list":
            result = [asdict(note) for note in store.list_notes()]
        elif args.command == "read":
            result = asdict(store.read(args.id))
        elif args.command == "archive":
            store.archive(args.id, revision=args.revision, unconditional=args.unconditional)
            result = {"id": args.id, "operation": args.command, "ok": True}
        elif args.command in {"push", "pull"}:
            result = WorkingCopy(notebook).transfer(
                args.command, args.files, all_files=args.all_files, dry_run=args.dry_run,
                show_diff=args.show_diff, title=getattr(args, "title", None),
                note_id=getattr(args, "note_id", None), unconditional=getattr(args, "unconditional", False))
        elif args.command == "search-title":
            result = [asdict(note) for note in notebook.search_title(args.query)]
        else:
            result = [asdict(note) for note in notebook.search_content(args.query)]
        print(json.dumps(result, ensure_ascii=False))
        if args.command in {"push", "pull"} and any(item["status"] in {"error", "conflict"} for item in result["results"]):
            return 1
        return 0
    except (ShyNoteError, OSError, UnicodeError) as exc:
        print(f"shynote: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("shynote: cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())

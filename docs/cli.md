# CLI reference

[Documentation](README.md)

Run `shynote COMMAND --help` for syntax. `python -m shynote` is equivalent when
using the installed Python environment. Commands emit JSON on stdout; prompts
and command-level errors go to stderr.

`--repo DIR` goes before the command. Init also accepts it after the command;
specify it only once. See [Configuration](configuration.md) for discovery and paths.

## init

```sh
shynote init [OPTIONS]
```

Prompts for missing settings when stdin is a terminal. Complete required flags
apply setup without prompting. Outside a terminal, missing settings are errors.

| Option | Meaning |
| --- | --- |
| `--backend aws\|s3\|notion` | Required; `aws` is saved as `s3` |
| `--bucket`, `--region` | Required for S3 |
| `--prefix` | S3 prefix; default `shynote` |
| `--endpoint-url` | Optional S3-compatible endpoint |
| `--parent-page` | Required for Notion; page URL or UUID |
| `--token-env` | Notion token variable name; default `NOTION_TOKEN` |
| `--notes-dir` | Relative notes directory; default `.agent/notes` |
| `--notebook` | Notebook identifier; default generated UUID |
| `--no-ignore-notes` | Omit the notes directory from new ignore rules |
| `--non-interactive` | Never prompt; fail on missing settings |
| `--repo` | Existing directory to initialize |

Backend-specific flags cannot be mixed. Init checks read access before writing
local configuration; empty storage succeeds. Write access is not tested. See
[Getting started](getting-started.md#initialize) for the setup sequence.

The result has status `initialized`, `already_initialized`, or `cancelled`.
An initialized result includes the repository and notes paths, backend, notebook,
storage settings, added ignore rules, warnings, `read_access: "verified"`, and
`write_access: "not_tested"`.

## push and pull

| Command | Action |
| --- | --- |
| `push FILE [FILE ...]` | Create remote notes for new files; update tracked notes |
| `push --all` | Push local Markdown files, including new files, plus existing tracked files |
| `pull --id NOTE_ID` | Restore a note at its saved relative path and begin tracking it |
| `pull FILE --id NOTE_ID` | Same, requiring FILE to match the saved remote path |
| `pull FILE [FILE ...]` | Refresh selected tracked files |
| `pull --all` | Refresh locally present tracked files |
| `pull --mirror` | Replace all local notes from upstream and rebuild tracking |

Paths are relative to `notes_dir`. Explicit lists preserve order, with duplicates
processed once. `push --all` recursively discovers `.md` files (case-insensitive)
under `notes_dir` and includes existing tracked paths regardless of extension.
Discovery excludes symbolic links and Git/ShyNote metadata; Git ignore rules do
not exclude notes. `pull --all` uses only tracked paths. Both use sorted paths,
report missing tracked files as skipped, and stop at the first error or conflict.
Earlier successes remain saved.

Use `push FILE --title TITLE` to create and track a new note. Creation and updates
both go through push; there is no separate `create` command.

| Option | Applies to | Action |
| --- | --- | --- |
| `--dry-run` | Push, pull | Preview changes and diffs without writes |
| `--diff` | Push, pull | Include diffs in transfer results |
| `--title TITLE` | First push of one file | Set the title; default is the file stem |
| `--id NOTE_ID` | Pull of one note | Select the remote note; FILE can be omitted |
| `--unconditional` | Push | Allow Notion updates without an atomic revision guard |

File arguments cannot be combined with `--all`. `--title` and `--id` apply only
to a single file and cannot rename or retarget an already tracked note.
`--unconditional` does not bypass detected content conflicts. S3 pushes always
use revision guards, including when this flag is supplied.

`pull --mirror` overwrites local edits and removes all local-only files within
`notes_dir`, including unpublished notes. It works without existing tracking and
replaces corrupted or mismatched state. A dedicated notes directory is required;
the project root (`notes_dir = "."`) is rejected. Combine it with `--dry-run` for a
preview; add `--diff` for preview or applied diffs. Do not combine it with FILE, `--all`, or `--id`.
Mirror downloads and validates the whole notebook before installing files;
preflight failures are command-level errors on stderr. Its JSON result adds
`mirror: true`. See [Replace from upstream](working-copy.md#replace-from-upstream).

The result contains `operation`, `dry_run`, and a `results` array. Each attempted
file has `file` and `status`, plus its known remote `id`, an `error` on failure, or
an available `diff` when requested. Diffs are JSON strings with escaped newlines.

An error may include a newly created `id` when remote creation succeeded but
verification failed; that note is not yet tracked locally. Inspect and recover
it with read/pull before retrying creation. Notion rate-limit wait notices go to
stderr while the request retries; exhausted retries produce an ordinary error.
See [Working with notes](working-copy.md) for examples and status meanings.

## Browse and read

| Command | Result |
| --- | --- |
| `info` | Local configuration and backend capabilities; no credentials required |
| `list` | Active note summaries |
| `read NOTE_ID` | Title, Markdown body, revision, and archive state |
| `search-title QUERY` | Matching active note summaries |
| `search-content QUERY` | Unsupported on both backends; exits with code 1 |

Summaries contain `id`, `title`, `path`, and `archived`. The required `path` is
relative to `notes_dir`; missing or invalid remote path metadata is an error.
Read results also contain `body`
and `revision`. `info` includes `root`, `notes_dir`, `notebook`, `backend`, and
capability flags.

S3 title search uses case-insensitive substring matching. Notion uses its own
title-search matching and may take time to index new pages. Blank queries are
rejected; use `list` to browse. A supported search with no matches returns `[]`.

## Archive

```sh
shynote archive NOTE_ID --revision 'REVISION_FROM_READ'
```

S3 archive retains the content, readable by ID, and hides the note from active
listings and search. Pass the exact revision from `read`, including any quotes
inside the value. Alternatively, `--unconditional` uses the revision read during
the operation instead of requiring one from the caller. S3 still guards the write
against changes after that read. The flags are mutually exclusive, and one is required.

Success returns `id`, `operation`, and `ok`. Notion archive is unsupported and
exits with code 1. There are no restore or delete commands.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Completed without errors or conflicts; inspect statuses for skipped work or cancelled init |
| `1` | Configuration, provider, unsupported-operation, or transfer failure |
| `2` | Invalid command-line syntax |
| `130` | Interrupted with Ctrl-C |

Command-level errors appear as `shynote: ...` on stderr. A per-file transfer
failure appears in the JSON results on stdout and stops the batch with code 1.
A write may have succeeded before readback or local tracking failed; inspect the
error and remote note before retrying.

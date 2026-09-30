# Working with notes

[Documentation](README.md)

Local Markdown files are scratch space until you push them. The first push
creates a remote note and tracks its ID; later pushes update that note. Pull
fetches a remote note into a local file and establishes the same tracking.

## Push and pull

Paths are relative to your configured notes directory, usually `.agent/notes`:

```sh
shynote push finding.md --title "Cache finding"
shynote pull --id NOTE_ID
```

After editing or when checking for remote changes:

```sh
shynote push finding.md --dry-run
shynote push finding.md
shynote pull existing.md --diff
```

For Notion updates, add `--unconditional` to push, including update previews.
It permits a write without an atomic revision guard; content conflicts still
stop the transfer. A first push creates a note without that flag.

Every remote note stores its path relative to `notes_dir`, separately from its
title. For example, `design/auth.md` and `research/auth.md` remain distinct even
if both titles are `auth`. `list`, `search-title`, and `read` include that path.

`pull --id NOTE_ID` restores the saved path, including its parent directories,
in a fresh checkout. You can also give `pull FILE --id NOTE_ID`, but FILE must
match the saved path. An initial pull accepts an absent file or an untracked file
whose content already matches the note. It refuses to overwrite a different draft.
Each remote note can be tracked by one local file per checkout. Subsequent pulls
use the saved ID. Moving a tracked file is not a remote rename operation.

Missing, malformed, or unsafe remote paths are errors. There is no title-based
fallback or automatic migration of notes created before path metadata existed.
Notion stores this metadata in the first code block on each page; leave it intact.
The block is excluded from local Markdown and diffs.

Notion also mirrors your directories as nested pages. For example,
`shynote push design/auth.md` creates or reuses the `design` page and places the
note inside it. Its default title is still `auth`; `--title` can change that
display title. List and search return notes from all ShyNote directory pages,
with folders themselves omitted. S3's object layout is unchanged.

Leave directory titles, parent relationships, and metadata intact. Manual moves
or directory renames that disagree with saved paths cause errors; there is no
automatic rename or move synchronization. Notion pages need an explicit note or
directory kind, so older metadata is not accepted. A failed push can leave empty
directory pages that later pushes reuse; it does not roll back created folders.

## Select files

```sh
shynote push hello1.md hello2.md
shynote pull hello1.md hello2.md --diff
shynote push --all --dry-run
shynote pull --all
```

Explicit lists run in argument order and process duplicate paths once.
`push --all` recursively includes new Markdown files under `notes_dir` and existing
tracked files, in sorted order. New files create remote notes; mapped files update
their existing notes. No previous push or pull is required for a Markdown file
to be selected.

Discovery accepts `.md` extensions case-insensitively, skips symbolic links and
Git/ShyNote metadata, and does not apply Git ignore rules. Tracked files with other
extensions remain included. Use explicit file arguments to publish only selected
scratch notes.

`pull --all` refreshes mapped files still present locally. It does not discover
remote notes or link new local files by name. Missing tracked files receive
`skipped_missing` on both commands. Local deletion never deletes or archives the
remote note.

Both forms stop at the first error or conflict. Results include earlier successes
and the failed file; later files are untouched and omitted. Earlier successful
transfers remain saved. A missing explicitly requested file is an error, except
when a first pull is creating it.

## Replace from upstream

To restore the whole notebook, or discard local work and rebuild tracking:

```sh
shynote pull --mirror --dry-run
shynote pull --mirror
```

**Upstream wins:** mirror downloads every active note at its saved path,
overwrites local edits, removes local-only files and empty directories, and
rebuilds `.shynote-local/state.json`. Missing, corrupted, or mismatched tracking
does not need manual repair or deletion. A new checkout can use the same command.
This includes removing non-Markdown files and notes that have never been pushed.
An empty upstream produces an empty notes directory.

Mirror requires a dedicated `notes_dir`; it refuses `notes_dir = "."`, symbolic
links, and nested Git/ShyNote metadata. Files outside that directory are preserved.
Use `--dry-run` to inspect overwrites and removals without changing anything, or
add `--diff` for preview or applied diffs. Do not combine `--mirror` with file
arguments, `--all`, or `--id`. It makes no remote writes.

All remote notes are downloaded and validated before replacing local files.
Missing/invalid metadata, duplicate remote paths, failed reads, or detected
concurrent changes abort the mirror. Installation errors trigger rollback; see
[Local persistence](storage.md#local-persistence) for crash limitations.

## Preview changes

`--dry-run` returns proposed changes and unified diffs without writing remote
notes, local files, tracking, or locks. It can still require credentials and
remote reads. `--diff` includes diffs during a normal transfer.

Read the status with the diff: a conflict diff shows the competing versions, not
an applied change. The real transfer checks again, so a preview does not reserve
a remote revision.

## Conflicts

ShyNote compares local and remote content with the last synchronized versions:

| Changes since the last transfer | Push | Pull |
| --- | --- | --- |
| Local only | Send local edits | Preserve them; report `local_changes` |
| Remote only | Preserve them; report `remote_changes` | Fetch remote edits |
| Both, with different content | Report `conflict` | Report `conflict` |
| Both now identical | Record them as synchronized | Record them as synchronized |

S3 checks the revision again atomically when writing. Notion cannot provide that
guarantee, so another writer can change the page between the check and an
unconditional write. See [Storage internals](storage.md#write-consistency) for
provider guarantees and Markdown normalization.

To resolve a conflict:

1. Save a copy of your local draft and inspect the remote body with `shynote read NOTE_ID`.
2. Make the tracked file match the reviewed remote body, then pull to establish that baseline.
3. Apply your intended edits, preview the push, and push again.

Further concurrent edits may produce another conflict. There is no automatic
merge. To discard local work in favor of the entire upstream notebook, use
`pull --mirror`. Deleting tracking alone does not resolve conflicts: a later push
could create a duplicate note.

Notion rate limits pause and retry the failed request automatically, within
bounded attempt and wait limits. Wait notices appear on stderr. If retries are
exhausted, the batch stops and earlier successful files remain tracked.

If a note was created but verification failed, its error result includes the
created `id`. Inspect it with `shynote read NOTE_ID`, then recover tracking with
`shynote pull --id NOTE_ID` before pushing again. Pull requires identical local
content or an absent file; preserve and reconcile any different draft first.
For errors from older versions that omitted the created ID, inspect `shynote list`
for the failed file's path. Do not assume a failed push means no page was created.

## Transfer statuses

| Status | Meaning |
| --- | --- |
| `created` / `would_create` | Created a remote note / preview of creation |
| `pushed` / `would_push` | Updated remote content / preview of update |
| `pulled` / `would_pull` | Fetched remote content / preview of fetch |
| `removed` / `would_remove` | Mirror removed a local-only file / preview of removal |
| `removed_directory` / `would_remove_directory` | Mirror removed an extra local directory / preview |
| `unchanged` | No content transfer needed; tracking may be refreshed |
| `local_changes` | Pull preserved local edits; push them when ready |
| `remote_changes` | Push preserved remote edits; pull them first |
| `skipped_missing` | `--all` skipped a tracked file absent locally |
| `conflict` | Divergent edits or a change during transfer; inspect the error |
| `error` | A failed operation; inspect the error |

Only `conflict` and `error` cause a per-file failure exit code. Preview statuses
are proposals, not guarantees. Some failures occur before a diff is available.

## Local state

`.shynote-local/state.json` stores note IDs, revisions, and synchronized content
hashes beside `.shynote`. It is private to this checkout and bound to the storage
location and notes directory. Missing notes-directory identity or a mismatch is
an error in ordinary transfers; configuration changes do not migrate state.
`pull --mirror` reconstructs this disposable state from the configured upstream.

ShyNote locks mutating transfers with `.shynote-local/lock`. If a process crashes,
confirm it has stopped before removing the leftover lock directory. Editors do
not honor this lock; avoid editing a file while pulling it.

Archived S3 notes remain readable but cannot be updated. Missing or archived
remote notes never cause local files to be deleted.

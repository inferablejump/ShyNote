# Migrate existing notes

[Documentation](README.md)

You can keep your current notes directory and make ShyNote its durable storage.
ShyNote's file-to-note mapping is independent of Git tracking. Init preserves
existing files and adds ignore rules, but does not upload or untrack them.

The examples below use `.agent/notes/`. Substitute your exact directory, including
capitalization. Run Git commands from the target repository root.

## Publish the existing notes

Back up the notes and inspect `git status --short` first. Separate unrelated
staged changes so they do not enter the migration commit. Choose a directory of
notes you intend to publish; `push --all` includes every Markdown file below it.

Initialize it using your chosen backend and existing credentials:

```sh
shynote init --notes-dir .agent/notes
```

In a terminal this runs the setup wizard. For scripts, use the
[noninteractive flags](getting-started.md#initialize). Leave notes ignoring enabled.
If the repository already has `.shynote`, inspect `shynote info`; init does not
change an existing notebook or migrate its tracking to a different directory.

If a local note already exists remotely but has no local mapping, use
`shynote pull --id NOTE_ID` to link it at its saved relative path. The local content
must match, or you must reconcile it first. ShyNote does not match notes by title;
pushing an unmapped file creates a new remote note. Existing remote notes without
path metadata are unsupported and error explicitly; this guide does not migrate
old remote storage formats.

Preview and publish:

```sh
shynote push --all --dry-run
shynote push --all
shynote list
```

Check each result before proceeding. A batch stops at its first failure; earlier
successes remain saved. For updates to already mapped Notion notes, add
`--unconditional` to push and its preview. New notes do not require it.

Only `.md` files are discovered automatically, case-insensitively; symbolic links
and Git/ShyNote metadata are excluded. Review any other files in the directory
separately before removing their Git tracking.
Verify important uploads with `shynote read NOTE_ID`.

## Stop tracking the directory in Git

An ignore rule does not affect files Git already tracks. After verifying the
notes are stored remotely, inspect which files Git tracks:

```sh
git ls-files -- .agent/notes/
```

If that lists files you intend to remove from Git, preview and stage the removal:

```sh
git rm -r --cached --dry-run -- .agent/notes/
git rm -r --cached -- .agent/notes/
git add -- .shynote .gitignore
git diff --cached --stat
git diff --cached -- .shynote .gitignore
```

`--cached` keeps files in your current working directory and removes them from
the Git index. Without it, `git rm` removes local files too. If Git refuses because
of staged changes, review those changes rather than adding `--force`.
See [git rm](https://git-scm.com/docs/git-rm) and
[gitignore](https://git-scm.com/docs/gitignore#_notes).

Confirm the expected ignore rules are present, including `/.shynote-local/`,
then commit the reviewed changes:

```sh
git commit -m "Move agent notes to ShyNote"
```

To undo the removal from the index before committing, use
`git restore --staged -- .agent/notes/`. The local files remain in place.

This removes notes from future Git snapshots, not existing history. Sensitive
content already committed remains in that history. Other checkouts applying the
removal commit may have their clean tracked copies removed, so teammates should
save local drafts first and can fetch published notes by ID afterward.

## Use another checkout

Install the CLI and agent skill, supply credentials, and use the committed
`.shynote` to find the same notebook:

```sh
shynote list
shynote pull --id NOTE_ID
```

Pull restores the saved directory layout under `notes_dir` and builds that
checkout's own tracking state. Do not copy `.shynote-local/`
between checkouts. `pull --all` refreshes mapped local files; it does not initially
download the remote notebook.

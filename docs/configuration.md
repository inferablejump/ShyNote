# Configuration

[Documentation](README.md)

## Repository and note paths

Commands look for the nearest `.shynote` in the current directory or its parents.
Use `shynote --repo DIR COMMAND` to start discovery elsewhere.

When no marker exists, `init` chooses the Git worktree root, or the current
directory outside Git. `shynote init --repo DIR` chooses an existing directory
explicitly. If a marker already exists in that directory or an ancestor, init
validates it and reports `already_initialized` without changing it.

The required `notes_dir` setting is relative to the directory containing
`.shynote`. Push and pull arguments are relative to `notes_dir`, even when invoked
from a subdirectory or with `--repo`.

For example, with `notes_dir = ".agent/notes"`:

```sh
shynote push findings/cache.md
```

reads `.agent/notes/findings/cache.md`. Absolute paths, parent traversal, metadata
paths, and symbolic links are rejected. To use the repository root, explicitly
set `notes_dir = "."`; during init, also pass `--no-ignore-notes`.

Missing `notes_dir` is an error. There is no fallback path. Changing it does not
move files or migrate tracking; tracking for a different notebook or notes
directory is rejected.

Missing-file and untracked-file errors include the absolute local path so you can
see where ShyNote looked.

## The `.shynote` file

Init writes TOML configuration. You can also write it manually. All top-level
fields shown below are required; unknown fields and unsupported versions are
rejected. Credentials belong outside this file.

### S3

```toml
version = 1
notebook = "my-project"
backend = "s3"
notes_dir = ".agent/notes"

[storage]
bucket = "your-bucket"
prefix = "shynote"
region = "us-east-1"
```

The storage namespace is the combination of bucket, prefix, and notebook ID.
This example stores notes under `shynote/my-project/notes/`. Init generates a
notebook UUID unless you supply `--notebook NAME`; allowed characters are letters,
digits, underscores, and hyphens. Copy the configuration to share that notebook
between checkouts.

`bucket` and `prefix` are required. `region` is required by init but optional in a
manually written marker, where SDK configuration can supply it. Optional
`endpoint_url` selects an S3-compatible endpoint; compatibility with its write
guards must be verified separately. HTTPS is required except for loopback tests.

AWS credentials are resolved by Boto3. ShyNote stores no access keys and does not
refresh credentials through its own login flow.

### Notion

```toml
version = 1
notebook = "my-project"
backend = "notion"
notes_dir = ".agent/notes"

[storage]
parent_page_id = "00000000-0000-4000-8000-000000000001"
token_env = "NOTION_TOKEN"
```

Replace the example UUID with your parent page ID. Init accepts a page URL and
converts it to a UUID. `parent_page_id` is required; `token_env` defaults to
`NOTION_TOKEN` and names the environment variable containing the token.

The parent page defines the notebook root. ShyNote creates directory pages below
it to mirror relative file paths, with notes inside those directories. Use a
dedicated parent: child pages must have ShyNote metadata identifying them as notes
or directories. Changing `notebook` alone does not create a separate Notion
notebook. ShyNote calls the Notion REST API directly.

## Git and local state

Commit `.shynote` and the ignore rules. By default, init adds:

```gitignore
/.shynote-local/
/.agent/notes/
```

A custom notes directory gets its own rule. `--no-ignore-notes` omits that rule;
it does not remove existing rules. Git-tracked files remain tracked.
To remove their Git tracking while keeping local copies, follow
[Migrate existing notes](migration.md).

`.shynote-local/state.json` holds file-to-note mappings and synchronization state.
It stays beside `.shynote`, regardless of the notes directory. Do not commit or
copy it between checkouts. For recovery, see [Local state](working-copy.md#local-state).

# Set up a ShyNote notebook

Use this reference when configuring a notebook is part of the user's task.
Reuse an existing `.shynote` when sharing a notebook between checkouts: running
init with a new generated S3 notebook ID would select a different namespace.

## Prerequisites

Use Python 3.11+ and an installed ShyNote CLI. From a ShyNote source checkout,
install the selected backend with `pip install -e '.[s3]'` or
`pip install -e '.[notion]'`. These commands require the package source directory;
do not run them in an arbitrary target repository.

Obtain the user's chosen storage location and use credentials from the environment:

- S3 needs an existing bucket and region. ShyNote uses the AWS SDK credential
  chain, including existing login sessions and `AWS_PROFILE`.
- Notion needs a parent page URL or UUID, access granted to the connection, and
  a token supplied through `NOTION_TOKEN` or a named `--token-env` variable.

Do not choose an account, provision storage, or start a login flow as a side effect
of using the notebook. Report missing or failed credentials without asking the
user to paste secrets into a note or config file.

## Initialize without prompts

Run in the target repository, using the supplied storage settings:

```sh
shynote init --non-interactive --backend aws --bucket YOUR_BUCKET --region YOUR_REGION
```

Or, with the Notion token already available:

```sh
shynote init --non-interactive --backend notion --parent-page YOUR_PAGE_URL_OR_UUID
```

`aws` is saved as `s3`. Init uses the Git worktree root, or the current directory
outside Git. Supply `--repo /path/to/repo` to select an existing directory.
A valid existing nearest marker is reported as `already_initialized` and left
unchanged. Invalid configuration fails instead of being repaired automatically.

Init writes `.shynote`, creates the notes directory (default `.agent/notes`), and
adds ignore rules for that directory and `.shynote-local/`. Existing files and
Git tracking are preserved; nothing is uploaded. Use `--notes-dir PATH` to select
another relative directory and `--no-ignore-notes` to omit its new ignore rule.
Using the root requires both `--notes-dir .` and `--no-ignore-notes`.

S3 accepts `--prefix` (default `shynote`) and `--notebook` (default generated UUID).
The bucket, prefix, and notebook ID together identify the S3 notebook. On Notion,
the parent page identifies the notebook; changing its local name does not isolate
pages. Use a dedicated parent for each independent notebook.

The access check accepts empty storage and makes no remote writes. Success
reports `read_access: "verified"` and `write_access: "not_tested"`. A failed check
leaves local setup unapplied. Run `shynote info` to read the resulting paths and
capabilities. Share `.shynote` and ignore rules as appropriate; keep credentials
and `.shynote-local/` out of Git.

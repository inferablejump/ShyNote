# Getting started

[Documentation](README.md)

## Install the CLI for use across repositories

ShyNote requires Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).
Clone the source and install it as a user-wide tool:

```sh
git clone https://github.com/inferablejump/ShyNote.git
cd ShyNote
uv tool install '.[s3,notion]'
shynote --help
```

Use `'.[s3]'` or `'.[notion]'` instead if you only need one backend. uv manages the
tool's environment and puts `shynote` on your PATH, so no venv activation is
needed in the repositories where you keep notes. See [uv's tool guide](https://docs.astral.sh/uv/guides/tools/).

If the command is not found, run:

```sh
uv tool update-shell
```

Then open a new terminal. If you previously activated the project's venv, run
`deactivate` to stop its executable from taking precedence. Check which install
you are using with `command -v shynote`.

### Work from an editable checkout

If you develop ShyNote or want source changes to take effect immediately, use an
editable tool install. From the ShyNote source directory:

```sh
uv tool install --editable '.[s3,notion]'
```

The same command works from anywhere with an absolute path:

```sh
uv tool install --editable '/absolute/path/to/ShyNote[s3,notion]'
```

Keep that checkout in place. After pulling changes, a normal installation needs
reinstalling from the source directory:

```sh
git pull --ff-only
uv tool install --reinstall '.[s3,notion]'
```

An editable install picks up Python source edits automatically. If package
metadata or dependencies change, rerun its install command with `--reinstall`
and `--editable`. These commands manage the CLI; skill copies are updated
separately.

To give a coding agent the workflow instructions, follow
[Install the agent skill](agent-skill.md). To bring existing notes into ShyNote,
follow [Migrate existing notes](migration.md).

## Prepare storage and credentials

**S3:** choose an existing bucket and its region. ShyNote uses the AWS SDK's
credential chain, including an existing AWS login session or a profile selected
with `AWS_PROFILE`. The S3 extra installs the SDK dependencies needed for login
sessions.

**Notion:** choose a parent page dedicated to this notebook, grant your connection
access to it, and supply the connection token in `NOTION_TOKEN`. Have the page URL
or UUID ready. Use a different parent page for each independent notebook.

ShyNote uses existing credentials. It does not run a login flow, ask for tokens,
or create a bucket or parent page.

## Initialize

In the repository where you want to keep notes, run:

```sh
shynote init
```

In a terminal, the wizard asks for missing settings, checks read access, and shows
the proposed configuration before asking you to apply it. Empty storage is valid.
The check makes no remote writes, so write permissions remain untested.

By default, setup creates these local files and directories:

| Path | Purpose |
| --- | --- |
| `.shynote` | Shared configuration; commit this file |
| `.agent/notes/` | Local Markdown scratch space |
| `.gitignore` | Rules for scratch notes and `.shynote-local/` tracking |

Existing notes are preserved. Init does not upload them or untrack files already
in Git. A failed access check or declined confirmation leaves setup unapplied.

For an agent or script, supply the required settings explicitly:

```sh
shynote init --non-interactive --backend aws --bucket YOUR_BUCKET --region us-east-1
```

Or, with `NOTION_TOKEN` already set:

```sh
shynote init --non-interactive --backend notion --parent-page YOUR_PAGE_URL_OR_UUID
```

Complete required flags skip the wizard even in a terminal. Missing settings are
an error with `--non-interactive` or when stdin is not a terminal.

Use `--notes-dir PATH` for another notes directory and `--no-ignore-notes` to omit
it from new ignore rules. See the [init reference](cli.md#init) for all options.

## Write and publish a note

With the default notes directory, run from the repository root:

```sh
printf '# Finding\n\nA useful observation.\n' > .agent/notes/finding.md
shynote push finding.md --title "First finding"
shynote list
```

The first push creates a remote note and records its ID locally. Subsequent
pushes update that note. To publish all local Markdown files, including new ones,
use `shynote push --all`. CLI file arguments are relative to the configured notes
directory, so `finding.md` means `.agent/notes/finding.md` here.

After editing the local file, preview and push the changes:

```sh
shynote push finding.md --dry-run
shynote push finding.md
```

For updates on Notion, add `--unconditional` to both commands. This accepts
Notion's lack of atomic write protection; detected content conflicts still stop
the transfer.

## Use the notebook in another checkout

Commit `.shynote` and `.gitignore`. In the other checkout, supply your credentials
and choose a note to fetch:

```sh
shynote list
shynote pull --id NOTE_ID
```

Pull uses the remote note's saved relative path and creates the local file and
its parent directories under `notes_dir`. Missing path metadata is an error;
older notes without it are unsupported. There is no initial
notebook download. Continue with [Working with notes](working-copy.md) for bulk
transfers, diffs, and conflict recovery.

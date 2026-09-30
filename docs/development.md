# Development

[Documentation](README.md)

## Run the offline suite

From the source checkout:

```sh
uv sync --all-extras
uv run --all-extras python -m unittest discover -v
```

Tests use the real adapters with [stateful service doubles](../tests/fakes.py),
plus botocore request validation for S3. The two fixture repositories at
`tests/fixtures/s3-repo/` and `tests/fixtures/notion-repo/` contain dummy settings.
No cloud credentials or live service calls are needed.

Coverage includes setup and cancellation, strict configuration, provider
capabilities, pagination, notebook isolation, working-copy transfers, diffs,
conflicts, Markdown normalization, and recovery of remote paths in a fresh
checkout, plus Notion directory creation, reuse, traversal, and hierarchy mismatch
errors. Missing and unsafe metadata must fail. Live provider behavior is checked
separately.
Rate-limit tests mock HTTP 429 responses and time: they verify retry timing and
limits, refusal to repeat ambiguous writes, and recovery from failed readback.
Bulk-push tests check that sibling discovery is shared within a batch and that
the cache is discarded afterward.
Mirror tests cover both adapters, missing/corrupt tracking, authoritative overwrites
and removals, fresh checkouts, dry runs, failed downloads, concurrent changes,
path collisions, filesystem rollback, and retaining backups when rollback fails.
CLI output tests cover compact bulk counts, visible failures and skipped work,
verbose results, and diffs that are computed only when requested.

## Live tests

Use disposable storage and credentials supplied outside the repository. These
scripts make real service calls and, except for the recorded init check, create
remote data.

### S3

```sh
uv run --extra s3 python -m tests.live_s3 --region us-east-1 --report /tmp/shynote-live-s3.json
```

Creates a temporary private bucket, exercises the CLI from separate checkouts,
and deletes the test objects and bucket in cleanup. The report records the AWS
identity, checks, and cleanup outcome. Normal request charges apply.

### Notion

Grant your connection access to a disposable parent page with no older notes
lacking path/kind metadata, and supply `NOTION_TOKEN`:

```sh
uv run --extra notion python -m tests.live_notion --parent-page-id PARENT_UUID --report /tmp/shynote-live-notion.json
```

Creates synthetic child notes and exercises search, Markdown, push/pull, diffs,
and conflicts across two temporary local checkouts, including discovery of new
files by `push --all`. It also checks nested page parents, directory reuse, and
path recovery. It leaves the remote notes and directories for manual cleanup and
records their IDs. Local test directories are removed.
`--token-env NAME` selects another token variable.

### Shared store contract

With two disposable directories already configured for the respective backends:

```sh
uv run --all-extras python -m tests.live_smoke --repo /tmp/shynote-s3 --repo /tmp/shynote-notion
```

Creates, reads, and updates a note on each backend through the store interface.
It archives the S3 note and verifies that Notion archive is unsupported. Both
remote notes remain stored; their IDs are printed.

## Recorded verification

These records describe runs on 2026-09-29, not a guarantee that every current
code path has been exercised live.

| Run | Result and scope |
| --- | --- |
| S3 | 13 checks passed for direct operations, isolation, search, write guards, and archive; resources cleaned up |
| Notion | 25 checks passed, including working copies, Markdown normalization, previews, and conflicts |
| Notion init | Two GET requests, no remote writes; temporary local setup removed |

Raw reports are local artifacts, excluded from Git because they contain account
and remote resource identifiers. Use the live scripts' `--report` option to save
results from your own runs.

The S3 run predates push/pull. The current S3 live script includes working-copy
checks, but that revision and S3 init have only offline coverage so far.
The Notion runs retained three synthetic notes across attempts.
New-file discovery by `push --all` is covered offline on both backends; the revised
Notion live script exercising that behavior has not been rerun.
Required remote paths and `pull --id` path recovery have offline coverage on both
backends; they have not been verified against live services yet.
Nested Notion directory pages are also covered offline; the updated live harness
has not been rerun for the hierarchy implementation.

Earlier Notion attempts led to the normalization handling described in
[Write consistency](storage.md#write-consistency).
The [AWS login region investigation](../.agent/NOTES/AWS_LOGIN_REGION.md) records
why the S3 adapter sets the region on both its session and client.

## Source map

| File | Responsibility |
| --- | --- |
| [cli.py](../shynote/cli.py) | Arguments, JSON output, and exit codes |
| [initialize.py](../shynote/initialize.py) | Setup wizard, access checks, and local setup |
| [config.py](../shynote/config.py) | Configuration discovery and validation |
| [notebook.py](../shynote/notebook.py) | Adapter selection |
| [model.py](../shynote/model.py) | Shared types, capabilities, protocol, and errors |
| [working_copy.py](../shynote/working_copy.py) | Tracking, diffs, transfer decisions, and local writes |
| [mirror.py](../shynote/mirror.py) | Authoritative upstream restore and tracking reconstruction |
| [stores/s3.py](../shynote/stores/s3.py) | S3 adapter |
| [stores/notion.py](../shynote/stores/notion.py) | Notion adapter and REST transport |
| [stores/notion_metadata.py](../shynote/stores/notion_metadata.py) | Required remote path block encoding and validation |
| [test_init.py](../tests/test_init.py) | Setup behavior and required notes-directory validation |
| [test_storage.py](../tests/test_storage.py) | Storage contract and provider behavior |
| [test_working_copy.py](../tests/test_working_copy.py) | Transfers and conflicts on both adapters |
| [test_mirror.py](../tests/test_mirror.py) | Upstream restore, previews, validation, and rollback |
| [test_cli_output.py](../tests/test_cli_output.py) | Compact summaries, verbose output, and explicit diffs |
| [test_paths.py](../tests/test_paths.py) | Fresh-checkout path recovery and malformed metadata rejection |
| [test_notion_hierarchy.py](../tests/test_notion_hierarchy.py) | Nested directory creation, traversal, isolation, and strict layout checks |
| [test_notion_rate_limits.py](../tests/test_notion_rate_limits.py) | Bounded 429 retries and preservation of created IDs after readback failure |
| [test_optional_dependencies.py](../tests/test_optional_dependencies.py) | Base install and missing-extra behavior |

## Keep documentation current

Update the relevant reference when behavior changes: command flags in
[CLI reference](cli.md), file settings in [Configuration](configuration.md),
transfer semantics in [Working with notes](working-copy.md), and provider contracts
in [Storage internals](storage.md). Keep setup examples short and runnable. Record
live test scope here and in the run reports rather than copying test history into
user guides.

# Storage internals

[Documentation](README.md)

## Layers

The CLI opens a `Notebook`, which owns the validated configuration and one `Store`.
Direct commands call the store. Push and pull go through `WorkingCopy`, which
handles local files, tracking, and conflict decisions for both backends.
`Store.create()` and `Store.update()` are internal operations used by push; the
CLI exposes neither as a separate command.

`Note` contains an opaque ID, title, Markdown body, revision, and archive state.
`NoteSummary` omits the body and revision. Callers do not interpret provider IDs
or revision tokens.

Opening a notebook performs no network requests. S3 creates its client on the
first remote operation; Notion reads its token when issuing a request. The base
package has no third-party dependencies. The S3 extra installs Boto3 with CRT;
the Notion adapter uses the standard library.

## Capabilities

| Operation | S3 | Notion REST |
| --- | --- | --- |
| Create, read, list, update body | Supported | Supported |
| Atomic conditional writes | ETag guards | Unsupported |
| Archive | Retained object, hidden from active results | Unsupported |
| Title search | LIST and HEAD metadata | REST search filtered to the parent page |
| Content search | Unsupported | Unsupported |

Capability flags are independent. Unsupported operations raise
`UnsupportedCapability` without provider calls. Authentication failures are
errors, never empty results. Archive retains content readable by ID; moving a
Notion page to trash does not satisfy that contract.

## Storage layout

S3 stores each note at `<prefix>/<notebook>/notes/<uuid>.md`. JSON inside
YAML-compatible frontmatter keeps metadata and body in one object write. Object
metadata also holds the title and archive flag for listing without body downloads.

Notion stores notes as direct child pages of the configured parent. Reads and
mutations check page membership. Non-page child blocks are ignored. The parent
page is the namespace; the local notebook name does not isolate pages.

See [Configuration](configuration.md) for the marker format and namespace settings.

## Access checks and search

`Store.check_access()` is used by init before saving configuration:

- S3 calls `ListObjectsV2` under the notebook prefix with `MaxKeys=1`.
- Notion retrieves the parent page, rejects an archived parent, and lists its
  children with `page_size=1`.

Empty results succeed. These checks neither fetch note bodies nor test write
permissions. Init skips remote checks for an existing valid configuration.

S3 listing uses paginated LIST requests and HEAD for each note object. Title
search filters those titles using case-insensitive substring matching, so its
request cost grows with notebook size.

Notion listing follows child-block pagination. Title search calls `POST /v1/search`
and filters the results to active direct children of the configured parent.
Matching and indexing follow the provider. Repeated or missing continuation
cursors and explicitly incomplete results are errors.

Both searches return summaries. Neither downloads note bodies or builds a local
index. `search_content` remains a separate unsupported operation.

## Write consistency

S3 creates use `If-None-Match: *`. Guarded updates and archives inspect the supplied
revision and then send `If-Match`, protecting the interval between read and write.
For an unconditional operation, the caller need not supply a revision, but the
S3 adapter still uses `If-Match` with the revision it just read.

Notion rejects conditional updates. For reads, it compares timestamps before and
after fetching Markdown and retries if they changed. This detects observed
changes but does not provide a transactional snapshot. Truncated or incomplete
Markdown is an error.

`WorkingCopy` records separate hashes for local and remote content, allowing
Notion to normalize Markdown while preserving local formatting. On update,
`Store.update()` returns the acknowledged Markdown: the submitted body on S3 or
the normalized body from Notion's mutation response. Push compares readback with
that acknowledgment. Different readback produces a conflict without advancing
tracking; an incomplete acknowledgment produces an error.

These errors can occur after a successful remote write. Ambiguous outcomes need
inspection before retrying; the Notion adapter does not automatically retry
creates.

## Local persistence

Tracking records the storage identity, required notes directory, and a mapping
from relative file paths to remote IDs, revisions, and synchronized hashes.
Corrupt or mismatched state fails explicitly.

State is replaced atomically after each successful transfer. A directory lock
serializes mutating ShyNote transfers within a checkout; other editors do not
participate in that lock. Local files are checked again before replacement.
A batch is a sequence of transfers, so earlier successes survive a later failure.
Dry runs create neither state nor locks.

For selection rules and conflict recovery, see [Working with notes](working-copy.md).
For coverage and live evidence, see [Development](development.md).

## Provider references

- [S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)
- [Notion Markdown API](https://developers.notion.com/guides/data-apis/working-with-markdown-content)
- [Notion Markdown write responses](https://developers.notion.com/reference/update-page-markdown)
- [Notion REST search](https://developers.notion.com/reference/post-search)

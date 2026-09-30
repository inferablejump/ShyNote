# Storage internals

[Documentation](README.md)

## Layers

The CLI opens a `Notebook`, which owns the validated configuration and one `Store`.
Direct commands call the store. Push and pull go through `WorkingCopy`, which
handles local files, tracking, and conflict decisions for both backends.
`Store.create()` and `Store.update()` are internal operations used by push; the
CLI exposes neither as a separate command.

`Note` contains an opaque ID, title, required relative path, Markdown body,
revision, and archive state.
`NoteSummary` omits the body and revision. Callers do not interpret provider IDs
or revision tokens.

Opening a notebook performs no network requests. S3 creates its client on the
first remote operation; Notion reads its token when issuing a request. The base
package uses tqdm for terminal progress. The S3 extra installs Boto3 with CRT;
the Notion adapter uses the standard library.

## Capabilities

| Operation | S3 | Notion REST |
| --- | --- | --- |
| Create, read, list, update body | Supported | Supported |
| Atomic conditional writes | ETag guards | Unsupported |
| Archive | Retained object, hidden from active results | Unsupported |
| Title search | LIST and HEAD metadata | REST search with notebook ancestry checks |
| Content search | Unsupported | Unsupported |

Capability flags are independent. Unsupported operations raise
`UnsupportedCapability` without provider calls. Authentication failures are
errors, never empty results. Archive retains content readable by ID; moving a
Notion page to trash does not satisfy that contract.

## Storage layout

S3 stores each note at `<prefix>/<notebook>/notes/<uuid>.md`. JSON inside
YAML-compatible frontmatter keeps metadata and body in one object write. Object
metadata also holds the base64-encoded title and relative path, plus the archive
flag, for listing without body downloads. Updates and archives preserve the path.

Notion mirrors directories as nested pages under the configured parent. Pushing
`design/auth.md` creates or reuses the `design` directory page, then creates the
note inside it. Directory titles match the exact local directory names; note
titles remain independent of filenames. The parent page is the namespace; the
local notebook name does not isolate pages.

Each Notion page starts with a plain-text code block containing
`shynote-metadata` on its first line and a JSON object such as
`{"version": 1, "kind": "note", "path": "design/auth.md"}` on its second line. This block is
visible in Notion and must remain first and intact. The adapter strips it from
returned Markdown and preserves it on updates. Directory pages use
`{"version": 1, "kind": "directory", "path": "design"}`. The configured notebook
root itself needs no metadata. Directory pages are excluded from note results
and cannot be read or updated as notes.
Child pages only support a title property, so the path is stored in this content
block rather than a custom page property.

Paths use `/` separators and are relative to `notes_dir`, with a maximum of 1024
UTF-8 bytes. Absolute paths, traversal, empty components, control characters,
backslashes, colons, and Git/ShyNote metadata directories are rejected. Missing
or invalid metadata fails explicitly on read, list, search, and update. Old notes
without paths are unsupported; there is no fallback or automatic migration.
Notion also requires an explicit `kind`; pages from older formats are not inferred
to be notes or directories.

Reads and updates follow page ancestry to the notebook root and verify the saved
path against directory metadata and titles. A manual move or directory rename
that disagrees with this layout is an error. Restore the original location/name
before retrying; ShyNote does not automatically move pages or rename local files.
Duplicate directory paths and paths occupied by both a note and a directory are
errors during directory discovery. Notes cannot act as directory pages.

Directory names must fit the 1–200 character title limit. Push creates missing
directories one at a time and only rewrites note content. Empty local directories
are not published. If a later write fails, earlier directory pages remain and can
be reused. This is not an atomic operation: concurrent creation can produce
duplicate directories, which a later discovery reports as ambiguous. There is
no automatic folder cleanup. A dry run creates no directories.

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

Notion listing follows child-block pagination through explicitly marked directory
pages, ignoring non-page blocks in those containers. Title search calls
`POST /v1/search` and checks result ancestry to include nested notes in this
notebook. Directory matches are excluded. Both listing and search read first
blocks for metadata; ancestry checks also retrieve parent pages. Creation reads
siblings to discover directories and reject ambiguous paths. Request counts grow
with notebook size and depth. During a push/pull batch, creation reuses sibling
discovery and adds newly created pages to that snapshot. Reused directories are
checked for changes before writing beneath them. The cache is discarded when
the transfer ends, including on error; reads and listings still perform fresh
checks. There is no persistent directory cache.
Matching and indexing follow the provider. Repeated or missing continuation
cursors and explicitly incomplete results are errors.

Both searches return summaries. Neither downloads note bodies or builds a local
index. `search_content` remains a separate unsupported operation.

## Notion rate limits

HTTP 429 responses are retried within the individual request, including reads
after creation. The transport respects `Retry-After`, adds a small random delay,
and uses exponential backoff when the header is missing or shorter. Each request
has at most five attempts and 120 seconds of cumulative retry waits. If the next
wait exceeds that budget, the request fails instead of retrying too early.
Wait notices go to stderr, leaving JSON output on stdout unchanged.

An explicit `public_api_request_blocked` response fails immediately. Other HTTP
errors and connection failures are not automatically retried; an ambiguous
write may already have succeeded. In particular, retrying a throttled readback
does not repeat the preceding page creation. If creation returned an ID but
verification still fails, the error preserves that ID for manual recovery.

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
Corrupt or mismatched state fails explicitly in ordinary transfers.

State is replaced atomically after each successful transfer. A directory lock
serializes mutating ShyNote transfers within a checkout; other editors do not
participate in that lock. Local files are checked again before replacement.
A batch is a sequence of transfers, so earlier successes survive a later failure.
Dry runs create neither state nor locks.

`pull --mirror` ignores old tracking and rebuilds it from every active remote note.
It validates unique, safe paths, downloads all bodies, and checks the listing again
before staging a replacement notes directory. It checks local files and state for
changes before installation, then swaps directories and writes the new state.
Installation exceptions trigger rollback; if rollback also fails, recovery files
are retained at paths reported in the error. A process crash or power failure is
not covered by this rollback: the directory and state are separate replacements.
After resolving any stale lock, another mirror can reconstruct both from upstream.
Remote listings and reads are not a transactional snapshot; detected changes abort,
but another writer can still change a note after it was read. Mirror represents the
downloaded active notes, not empty Notion directory pages or archived notes.

For selection rules and conflict recovery, see [Working with notes](working-copy.md).
For coverage and live evidence, see [Development](development.md).

## Provider references

- [S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)
- [Notion Markdown API](https://developers.notion.com/guides/data-apis/working-with-markdown-content)
- [Notion Markdown write responses](https://developers.notion.com/reference/update-page-markdown)
- [Notion code block format](https://developers.notion.com/guides/data-apis/enhanced-markdown)
- [Notion page properties](https://developers.notion.com/reference/post-page)
- [Notion child-page traversal](https://developers.notion.com/reference/get-block-children)
- [Notion request limits](https://developers.notion.com/reference/request-limits)
- [Notion REST search](https://developers.notion.com/reference/post-search)

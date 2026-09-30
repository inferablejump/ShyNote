# Design decisions

[Documentation](README.md)

These decisions explain the current behavior. Command syntax belongs in the
[CLI reference](cli.md); the [original idea](../.agent/NOTES/IDEA.md) describes the
broader product direction.

## One backend per notebook

S3 and Notion share a Python store interface. Developing and testing both keeps
the interface independent of one provider. Each notebook selects exactly one
backend; replication between backends and failover are outside the design. Dependencies are grouped
by backend so an installation only needs the SDKs it uses.

## Local notes are explicit working copies

Scratch files become durable through push. Pull brings selected notes back into
a checkout. `push --all` includes new local Markdown files as well as mapped
files: a remote mapping decides whether to create or update, not whether a file
is eligible to push. `pull --all` needs those mappings to refresh existing local
files. It does not download the whole notebook. Deleting a local file has no
remote effect.

`pull --mirror` explicitly makes upstream authoritative for the entire notes
directory. It restores all active notes, discards local-only work, and rebuilds
tracking from remote IDs, paths, revisions, and contents. Local state is disposable:
a missing or corrupted state file must not require manual mapping repair.

Push and pull name the transfer direction. Push creates a remote note on first
use and updates it afterward, keeping both operations in the tracking workflow.
Creation and body updates are internal store operations; neither has a separate
public command. There is no background synchronization.

## Configuration must be explicit

`.shynote` records the backend and required `notes_dir`. Init writes a default
notes directory, but reading a config never invents one. Missing fields and
tracking identity mismatches are errors in ordinary transfers. This prevents a configuration change
from silently targeting different files.

Configuration is shared through Git. Local tracking and scratch notes are ignored
by default because they belong to a checkout's working session; users may choose
to keep the notes themselves in Git.

## Setup reuses existing storage and credentials

Init offers a wizard for humans and complete flags for scripts and agents. It
checks read access before applying local setup. Empty storage must work, so the
check does not depend on an existing note. It makes no probe writes and cannot
claim to verify write permissions.

Login and remote provisioning remain outside ShyNote. Existing credentials are
used when needed; opening a configured notebook stays a local operation.

## Capabilities have consistent meanings

Title and content search are independent operations. An unsupported capability
raises an error instead of returning an empty result or choosing another search
strategy. Neither backend requires a local index to support title search.

Archive means retained content that remains readable by ID but disappears from
active results. S3 supports that behavior. Notion archive is unsupported because
moving a page to trash does not meet the retention contract.

## Transfers expose conflicts and partial progress

Compare each side with the last synchronized content and report divergence.
S3 protects writes with revision conditions; Notion updates require explicit
acceptance of its weaker write guarantee. Neither path silently resolves a
content conflict.

Diffs and dry runs make changes reviewable before writing. The actual transfer
checks again. Multi-file transfers execute in order and stop at the first error
or conflict, preserving earlier successes. This keeps failure behavior the same
for an explicit file list and `--all` without requiring a batch transaction.

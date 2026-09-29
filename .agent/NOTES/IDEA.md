# ShyNote

This document records the product idea, including features beyond the current
implementation. For working commands, configuration, and current status, use the
[project documentation](../../docs/README.md). Agreed behavior is recorded in
[design decisions](../../docs/decisions.md).

## Overview

ShyNote is an open-source persistent notebook for coding agents.

Coding agents routinely produce useful knowledge that does not belong in the source repository: debugging findings, failed approaches, architectural observations, experiment results, temporary plans, operational gotchas, and other working notes. Today this information is usually kept in an ad hoc local file, buried in a chat transcript, committed into Git where it creates noise, or simply lost when the agent session ends.

ShyNote provides a simple shared place for this knowledge to live.

The core abstraction is:

> **A persistent, repo-scoped notebook for coding agents, separate from source control.**

Humans continue using Git normally. Agents get a durable notebook associated with the repository.

---

## Goals

ShyNote should make persistent agent knowledge feel almost invisible to both humans and models.

An agent should be able to:

- discover that a repository has a ShyNote notebook,
- read relevant existing notes,
- create or update notes,
- search historical knowledge,
- associate notes with useful context such as files or Git commits,
- and continue working across sessions and machines.

A human should ideally only need to enable ShyNote once.

After that, normal workflow should remain:

```text
git clone ...
cd repo
codex / claude / cursor / ...
```

The agent should not need to understand storage infrastructure, synchronization protocols, databases, or complicated version-control semantics.

---

## Motivation

Modern coding agents generate substantial engineering knowledge outside the final code change.

Examples include:

- why a particular design was rejected,
- which debugging paths were already attempted,
- benchmark results,
- undocumented repository conventions,
- surprising interactions between components,
- commands required to reproduce an environment,
- assumptions made during an implementation,
- recurring mistakes,
- and context that would otherwise need to be rediscovered.

This information is useful, but does not naturally belong in Git.

Source control answers:

> What changed?

ShyNote should help answer:

> What have agents learned while working on this repository?

---

## Design Principles

### 1. Git remains for source code

ShyNote should not attempt to replace Git or become another full version-control system.

A note may record the Git commit, branch, or relevant source paths that were current when it was written, but this is provenance rather than synchronization semantics.

ShyNote does not need branches, rebases, merges, or a parallel Git DAG.

### 2. Agent-first ergonomics

Agents should interact with a very small conceptual surface.

At the highest level:

```text
read
write
search
archive
```

Everything else should remain an implementation detail.

The model should perceive something closer to:

> “Here is your persistent notebook.”

rather than:

> “Here is a distributed storage system you must operate correctly.”

### 3. Structured enough to retrieve, loose enough to write naturally

Notes should primarily be ordinary text or Markdown, with lightweight metadata.

For example:

```yaml
title: TLB refill race investigation
created_at: 2026-09-28
git_sha: 8f21ab3
paths:
  - src/tlb.scala
tags:
  - tlb
  - race
status: active
```

followed by arbitrary Markdown.

The schema should provide useful provenance without forcing agents into rigid templates.

### 4. Storage is pluggable

ShyNote should define the notebook abstraction independently from the backing store.

Possible backends include:

- local filesystem,
- S3-compatible object storage,
- Google Drive / Google Docs,
- Notion,
- database-backed storage,
- self-hosted object stores such as MinIO.

A team should be able to use infrastructure it already owns.

### 5. Open and portable

The format and protocol should be open.

A user's notes should never become dependent on a hosted ShyNote service existing forever.

Where practical, storage formats should remain straightforward enough that users can inspect or export their data without specialized infrastructure.

### 6. Customer/user data is not training data

ShyNote itself does not use stored content for training models.

Users may explicitly use their own ShyNote data for their own training, evaluation, analysis, or skill generation.

---

## Repository Association

A repository may contain a small tracked marker identifying that ShyNote is enabled.

For example:

```text
.shynote
```

This file may contain only minimal configuration such as:

```toml
version = 1
notebook = "repo-123"
```

This allows a newly cloned repository to automatically reconnect to the correct notebook.

A typical workflow might be:

```text
git clone repo
cd repo
agent starts
↓
ShyNote integration detects .shynote
↓
notebook becomes available
```

No explicit initialization should normally be required after the repository has been enabled once.

---

## Agent Interface

There are two reasonable ways to expose ShyNote.

### Filesystem view

The agent may see a normal directory such as:

```text
.agent/
  architecture.md
  debugging/
  experiments/
  gotchas.md
```

This could be:

- the actual storage,
- a local cache,
- or a materialized view of a remote notebook.

The agent does not need to know which.

### Tool interface

Agents may additionally receive a very small MCP or native tool interface:

```text
search_title(query)
search_content(query)
read_note(id)
create_note(...)
update_note(...)
archive_note(id)
```

Title search and content search are separate optional capabilities. If a backend
does not support one, return an explicit unsupported error for that operation.
Never silently substitute title search for content search or download the whole
notebook as a fallback. The complete notebook may eventually become much larger
than the context an agent can inspect directly.

---

## Notes and Metadata

A ShyNote entry is conceptually a document plus metadata.

Potential metadata includes:

```text
note ID
title
creation/update time
repository ID
Git commit SHA
branch
relevant source paths
tags
authoring agent/session
status
relationships to other notes
```

Relationships may eventually support concepts such as:

```text
supersedes
related_to
derived_from
contradicts
```

These should remain optional.

The design should prefer simple documents over complicated knowledge-graph structures unless usage demonstrates a need for them.

---

## Retrieval

As notebooks grow, simply reading every note becomes impractical.

ShyNote may provide retrieval services such as:

- full-text search,
- semantic search,
- filtering by repository or source path,
- filtering by Git commit or time,
- recency ranking,
- relevance to currently edited files,
- retrieval of related notes.

An agent might ask:

```text
What have previous agents discovered about TLB shootdowns?
```

or:

```text
Are there any notes related to src/cache.scala?
```

The retrieval layer should return both content and provenance so the agent can reason about whether a note is still applicable.

For example:

```text
Finding:
The refill path can deadlock if X occurs after Y.

Context:
Created against commit 8f21ab3
Relevant path: src/tlb.scala
Created: 2026-04-17
```

---

## Synchronization

ShyNote should support multiple machines and multiple agents accessing the same notebook.

The consistency model does not need to be elaborate initially.

Most notes are likely to be:

- created once,
- updated occasionally,
- and read much more frequently than concurrently edited.

Simple document-level conflict detection may therefore be sufficient.

The project should avoid introducing distributed-systems complexity until actual usage requires it.

---

## Possible Backends

### Local filesystem

Useful for:

- individual experimentation,
- development,
- fully offline workflows.

### S3-compatible storage

Likely the simplest general-purpose backend.

Advantages include:

- low cost,
- easy self-hosting,
- broad provider support,
- simple ownership model,
- good durability.

### Google Drive / Docs

Potentially useful for teams that want:

- human-readable notes,
- comments,
- existing sharing controls,
- built-in document history.

### Notion

Potentially useful for:

- structured databases,
- human browsing,
- tagging,
- collaborative editing.

The exact backend should not affect agent-facing semantics.

---

## Derived Services

Once a useful corpus of agent notes exists, several optional higher-level services become possible.

### Skill extraction

ShyNote can identify recurring lessons such as:

- common failure modes,
- repository-specific procedures,
- commands that agents repeatedly rediscover,
- patterns that frequently lead to mistakes.

These could be proposed as reusable skills or instructions.

For example:

```text
.agent/skills/
  modifying-generated-code.md
  running-firesim-tests.md
  debugging-tlb-refills.md
```

Generated skills should retain provenance and ideally be reviewed rather than silently becoming authoritative.

### Dataset generation

Users may expose their own notes as structured datasets.

A dataset might pair:

```text
repository state
+
task
+
agent notes
+
implementation
+
outcome
```

This could support:

- fine-tuning,
- evaluation,
- error analysis,
- agent research,
- organization-specific models.

A streaming dataloader API may eventually be useful so users can consume the corpus without exporting everything manually.

### Organizational retrieval

For larger teams, ShyNote could become a searchable repository of accumulated engineering knowledge across many projects.

Cross-repository retrieval is useful, but should be treated as a higher-level capability rather than part of the minimal core.

---

## Privacy and Security

ShyNote may contain information more sensitive than the Git repository itself.

Agent notes could contain:

- unreleased designs,
- debugging logs,
- customer information,
- internal experiments,
- operational details,
- accidentally copied secrets.

The design should therefore assume the notebook is confidential.

Principles include:

- no use of notebook contents for ShyNote model training,
- support for user-controlled storage,
- minimal centralized infrastructure,
- encryption where appropriate,
- explicit repository/team access control,
- easy deletion and export.

A BYO-storage deployment should be able to operate without a ShyNote-hosted data plane.

---

## Non-Goals

ShyNote is not initially intended to be:

- a replacement for Git,
- an automatic transcript recorder,
- a full agent-observability system,
- a chat-history service,
- a general-purpose knowledge-management platform,
- a model-training service,
- or a sophisticated distributed database.

It should solve one narrow problem well:

> Give coding agents a persistent notebook associated with a repository.

---

## Minimal First Version

The first implementation will support **Notion and S3**, developed together in
**Python** behind a shared storage interface. Each notebook selects **exactly one**
backing storage provider. This is not a multi-backend synchronization or mirroring
system. Two separate dummy repositories, each with its own `.shynote` configuration,
exercise the same behavior against different adapters.

Opening a notebook must remain lazy. Title search and content search are separate
optional operations, and unsupported operations report that the selected backend
does not support them. Both backends support title search: Notion through its REST
search endpoint, S3 through object listing and title metadata filtering. Content
search is unsupported on both. No initial notebook download, local search index,
or hosted MCP integration is required as a fallback.

See [the storage design](../../docs/storage.md) for the initial contract, provider
capabilities, fixtures, and limitations. The current implementation is the storage
foundation; it does not yet implement the entire MVP below.

The intended MVP includes:

```text
1. .shynote repository marker
2. local .agent/ notebook
3. simple note metadata schema
4. Notion and S3 adapters (one provider per notebook)
5. Separate title/content search with explicit backend capabilities
6. MCP/CLI interface
```

For example, the current CLI supports:

```bash
shynote search-title "cache coherence"
shynote push finding.md
shynote pull --all --diff
```

`shynote init` supports guided or noninteractive setup, with `.agent/notes` as
the default local notes directory. MCP integration is planned. `search-content` currently
returns unsupported on both adapters. Explicit `push` and `pull` implement the
local working-copy workflow; there is no `sync` command.

An agent integration could require little more than:

```text
This repository has persistent notes available through ShyNote.
Read relevant prior notes when useful and record durable findings that
future agents are likely to benefit from.
```

That may be enough to test whether the abstraction is useful before designing anything more complicated.

---

## Long-Term Direction

If the idea works, ShyNote could become a small interoperable convention for persistent agent knowledge.

Different coding agents could share the same notebook. Different organizations could choose different storage backends. Higher-level tools could build retrieval, skill generation, evaluation, and training pipelines on top of the same underlying data.

The value would come less from novel storage technology and more from establishing a simple common abstraction:

> **Source code lives in Git. Durable agent knowledge lives in ShyNote.**

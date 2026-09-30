import json
import os
import posixpath
import random
import sys
import time
from contextlib import contextmanager
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import UUID

from . import notion_metadata

from ..config import NotebookConfig, NotionConfig
from ..model import (Capabilities, Conflict, CreatedNoteError, Note, NoteSummary, NotFound,
                     ProviderError, ShyNoteError, UnsupportedCapability,
                     validate_path, validate_title, validate_write)


class NotionTransport:
    """Direct REST transport. No hosted MCP or model calls."""

    max_attempts = 5
    max_retry_wait = 120

    def __init__(self, token_env: str):
        self.token_env = token_env

    def request(self, method: str, path: str, payload=None):
        token = os.environ.get(self.token_env)
        if not token:
            raise ProviderError(f"Set {self.token_env} to a Notion API token.")
        request = Request(
            f"https://api.notion.com/v1/{path}", method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": f"Bearer {token}", "Notion-Version": "2026-03-11",
                     "Content-Type": "application/json"},
        )
        waited = 0
        for attempt in range(self.max_attempts):
            try:
                with urlopen(request, timeout=30) as response:
                    return json.load(response)
            except HTTPError as exc:
                status = exc.code
                retry_after = exc.headers.get("Retry-After")
                blocked = False
                try:
                    if status == 429:
                        error = json.load(exc)
                        blocked = error.get("additional_data", {}).get("rate_limit_reason") == "public_api_request_blocked"
                except (ValueError, TypeError, AttributeError):
                    pass  # Error responses need not have a JSON body.
                finally:
                    exc.close()
                if status == 404:
                    raise NotFound("Note not found or not accessible.") from exc
                if status == 409:
                    raise Conflict("Notion reported a conflict.") from exc
                if blocked:
                    raise ProviderError("Notion API access is restricted (HTTP 429, public_api_request_blocked); automatic retries will not help.") from exc
                if status == 429 and attempt + 1 < self.max_attempts:
                    backoff = 2 ** attempt
                    try:
                        delay = max(int(retry_after), backoff)
                    except (ValueError, TypeError):
                        delay = backoff
                    delay += random.uniform(0, 0.25)
                    if waited + delay <= self.max_retry_wait:
                        print(f"shynote: Notion rate limit; waiting {delay:.1f}s before retry {attempt + 1}/{self.max_attempts - 1} ({method} {path}).",
                              file=sys.stderr, flush=True)
                        time.sleep(delay)
                        waited += delay
                        continue
                suffix = f" Retry after {retry_after} seconds." if status == 429 and retry_after else ""
                if status == 429:
                    suffix += " Automatic retry limit reached."
                elif method not in {"GET", "HEAD"} and status >= 500:
                    suffix += " A write may have succeeded; inspect before retrying."
                raise ProviderError(f"Notion request failed (HTTP {status}).{suffix}") from exc
            except URLError as exc:
                raise ProviderError("Cannot reach Notion. A failed write may have succeeded; inspect before retrying.") from exc


class NotionStore:
    capabilities = Capabilities(conditional_writes=False, title_search=True)

    def __init__(self, config: NotebookConfig, *, transport=None):
        assert isinstance(config.storage, NotionConfig)
        self.settings = config.storage
        self.transport = transport if transport is not None else NotionTransport(self.settings.token_env)
        self._discovery_cache = None

    @contextmanager
    def transfer_batch(self):
        """Reuse sibling discovery within one transfer, never across commands."""
        self._discovery_cache = {}
        try:
            yield
        finally:
            self._discovery_cache = None

    def _id(self, note_id: str) -> str:
        try:
            return str(UUID(note_id))
        except ValueError as exc:
            raise NotFound("Invalid note ID.") from exc

    def check_access(self):
        parent = self.settings.parent_page_id
        page = self.transport.request("GET", f"pages/{parent}")
        if self._archived(page):
            raise ProviderError("The configured Notion parent page is archived or in trash.")
        self.transport.request("GET", f"blocks/{parent}/children?page_size=1")

    def _page(self, note_id: str):
        return self.transport.request("GET", f"pages/{self._id(note_id)}")

    def _parent_path(self, page):
        """Check ancestry before interpreting any ancestor as a ShyNote directory."""
        root = self.settings.parent_page_id
        seen = {self._id(page["id"])}
        if root in seen:
            raise NotFound("The notebook root is not a note.")
        ancestors = []
        parent = page.get("parent", {})
        while True:
            if parent.get("type") != "page_id":
                raise NotFound("Note is outside this notebook.")
            parent_id = self._id(parent["page_id"])
            if parent_id == root:
                break
            if parent_id in seen:
                raise ProviderError("Notion returned a cycle in page ancestry.")
            seen.add(parent_id)
            ancestor = self._page(parent_id)
            if self._archived(ancestor):
                raise NotFound("Note is inside an archived directory.")
            ancestors.append(ancestor)
            parent = ancestor.get("parent", {})
        path = ""
        for ancestor in reversed(ancestors):
            metadata = self._metadata(self._id(ancestor["id"]))
            if metadata["kind"] != "directory":
                raise ProviderError(f"Notion page {ancestor['id']} is not a ShyNote directory; notes cannot contain notebook folders or notes.")
            self._check_location(ancestor["id"], self._title(ancestor), metadata, path)
            path = metadata["path"]
        return path

    @staticmethod
    def _check_location(page_id, title, metadata, parent_path):
        path = metadata["path"]
        if (posixpath.dirname(path) != parent_path
                or (metadata["kind"] == "directory" and posixpath.basename(path) != title)):
            raise ProviderError(f"Notion page {page_id}: hierarchy does not match saved path {path!r}. Restore its original parent and directory title; automatic moves and renames are not supported.")

    @staticmethod
    def _archived(page):
        return bool(page.get("in_trash") or page.get("is_archived") or page.get("archived"))

    @staticmethod
    def _title(page):
        return "".join(text.get("plain_text", text.get("text", {}).get("content", ""))
                       for prop in page["properties"].values() if prop.get("type") == "title"
                       for text in prop["title"])

    def create(self, title: str, body: str, *, path: str) -> Note:
        validate_title(title)
        validate_path(path)
        directories = path.split("/")[:-1]
        # Validate every directory name before creating any remote pages.
        for directory in directories:
            validate_title(directory)
        parent = self._ensure_directories(directories)
        if any(metadata["kind"] == "directory" and metadata["path"] == path
               for _, _, metadata in self._entries(parent, posixpath.dirname(path), discovery=True)):
            raise ProviderError(f"Cannot create note {path!r}: its remote path is occupied by a directory.")
        page = self._create_page(parent, title, notion_metadata.encode(body, path))
        try:
            note = self.read(page["id"])
        except (ShyNoteError, OSError, UnicodeError) as exc:
            raise CreatedNoteError(page["id"], f"Created Notion note {page['id']}, but verification failed: {exc} Inspect it with read, then use pull --id {page['id']} to recover tracking; do not repeat the create.") from exc
        if self._discovery_cache is not None and parent in self._discovery_cache:
            self._discovery_cache[parent].append((note.id, note.title, {"kind": "note", "path": note.path}))
        return note

    def _create_page(self, parent, title, markdown):
        return self.transport.request("POST", "pages", {
            "parent": {"type": "page_id", "page_id": parent},
            "properties": {"title": {"type": "title", "title": [
                {"type": "text", "text": {"content": title}}]}},
            "markdown": markdown,
        })

    def _ensure_directories(self, directories):
        parent, parent_path = self.settings.parent_page_id, ""
        for title in directories:
            path = posixpath.join(parent_path, title)
            entries = self._entries(parent, parent_path, discovery=True)
            matches = [(page_id, metadata) for page_id, _, metadata in entries
                       if metadata["path"] == path]
            if matches:
                if len(matches) != 1 or matches[0][1]["kind"] != "directory":
                    raise ProviderError(f"Cannot use {path!r} as a directory: its remote path is ambiguous or occupied by a note.")
                parent = matches[0][0]
                # Cached discovery must not let a moved/renamed directory redirect writes.
                current = self._page(parent)
                metadata = self._metadata(parent)
                if self._archived(current):
                    raise NotFound(f"Directory {path!r} is archived.")
                if metadata["kind"] != "directory" or metadata["path"] != path:
                    raise ProviderError(f"Directory {path!r} changed; inspect its metadata before retrying.")
                self._check_location(parent, self._title(current), metadata, self._parent_path(current))
            else:
                page = self._create_page(parent, title, notion_metadata.encode("", path, kind="directory"))
                directory_id = self._id(page["id"])
                try:
                    metadata = self._metadata(directory_id)
                except (ShyNoteError, OSError, UnicodeError) as exc:
                    raise ProviderError(f"Created Notion directory {directory_id} for {path!r}, but verification failed: {exc} Inspect it before retrying.") from exc
                if metadata["kind"] != "directory" or metadata["path"] != path:
                    raise ProviderError(f"New directory page {directory_id} has incorrect metadata; inspect it before retrying.")
                if self._discovery_cache is not None:
                    self._discovery_cache[parent].append((directory_id, title, metadata))
                parent = directory_id
            parent_path = path
        return parent

    def read(self, note_id: str) -> Note:
        note_id = self._id(note_id)
        for _ in range(3):
            before = self._page(note_id)
            self._parent_path(before)
            content = self.transport.request("GET", f"pages/{note_id}/markdown")
            after = self._page(note_id)
            if (before["last_edited_time"] != after["last_edited_time"]
                    or before["parent"] != after["parent"]):
                continue
            if content.get("truncated") or content.get("unknown_block_ids"):
                raise ProviderError("Notion returned incomplete Markdown; refusing a partial note.")
            try:
                body, path = notion_metadata.decode(content["markdown"])
            except ShyNoteError as exc:
                raise ProviderError(f"Notion page {note_id}: {exc}") from exc
            self._check_location(note_id, self._title(after), {"kind": "note", "path": path},
                                 self._parent_path(after))
            return Note(note_id, self._title(after), body, after["last_edited_time"], path, self._archived(after))
        raise Conflict("The note kept changing while it was being read.")

    def _metadata(self, note_id):
        # Only fetch the first block, not the complete note body.
        result = self.transport.request("GET", f"blocks/{note_id}/children?page_size=1")
        blocks = result["results"]
        if not blocks or blocks[0]["type"] != "code":
            raise ProviderError(f"Missing ShyNote path metadata on Notion page {note_id}.")
        text = "".join(item.get("plain_text", item.get("text", {}).get("content", ""))
                       for item in blocks[0]["code"]["rich_text"])
        try:
            return notion_metadata.decode_metadata(text)
        except ShyNoteError as exc:
            raise ProviderError(f"Notion page {note_id}: {exc}") from exc

    def _children(self, parent_id):
        cursor = None
        seen = set()
        while True:
            query = {"page_size": 100}
            if cursor:
                query["start_cursor"] = cursor
            page = self.transport.request("GET", f"blocks/{parent_id}/children?{urlencode(query)}")
            for block in page["results"]:
                if block["type"] == "child_page" and not self._archived(block):
                    yield self._id(block["id"]), block["child_page"]["title"]
            if not page.get("has_more"):
                return
            cursor = page.get("next_cursor")
            if not cursor or cursor in seen:
                raise ProviderError("Notion returned an invalid pagination cursor.")
            seen.add(cursor)

    def _entries(self, parent_id, parent_path, *, discovery=False):
        cache = self._discovery_cache if discovery else None
        if cache is not None and parent_id in cache:
            return cache[parent_id]
        entries = []
        directory_paths = set()
        for page_id, title in self._children(parent_id):
            metadata = self._metadata(page_id)
            self._check_location(page_id, title, metadata, parent_path)
            if metadata["kind"] == "directory":
                if metadata["path"] in directory_paths:
                    raise ProviderError(f"Ambiguous Notion directory path {metadata['path']!r}; multiple directory pages exist.")
                directory_paths.add(metadata["path"])
            entries.append((page_id, title, metadata))
        if any(metadata["kind"] == "note" and metadata["path"] in directory_paths
               for _, _, metadata in entries):
            raise ProviderError("A Notion path is occupied by both a note and a directory.")
        if cache is not None:
            cache[parent_id] = entries
        return entries

    def list_notes(self) -> list[NoteSummary]:
        notes = []
        pending = [(self.settings.parent_page_id, "")]
        seen = {self.settings.parent_page_id}
        while pending:
            parent_id, parent_path = pending.pop()
            for page_id, title, metadata in self._entries(parent_id, parent_path):
                if page_id in seen:
                    raise ProviderError("Notion returned a repeated page in the notebook hierarchy.")
                seen.add(page_id)
                if metadata["kind"] == "directory":
                    pending.append((page_id, metadata["path"]))
                else:
                    notes.append(NoteSummary(page_id, title, metadata["path"]))
        return notes

    def search_title(self, query: str) -> list[NoteSummary]:
        if not isinstance(query, str) or not query.strip():
            raise ShyNoteError("Title search requires a nonempty query. Use list to browse notes.")
        payload = {"query": query.strip(), "filter": {"property": "object", "value": "page"},
                   "page_size": 100}
        notes = []
        seen_cursors = set()
        seen_ids = set()
        while True:
            result = self.transport.request("POST", "search", payload)
            if result.get("request_status", {}).get("type") == "incomplete":
                raise ProviderError("Notion returned incomplete search results. Narrow the title query.")
            for page in result["results"]:
                parent = page.get("parent", {})
                if page.get("object") != "page" or self._archived(page) or parent.get("type") != "page_id":
                    continue
                note_id = self._id(page["id"])
                if note_id not in seen_ids:
                    seen_ids.add(note_id)
                    try:
                        parent_path = self._parent_path(page)
                    except NotFound:
                        # Search spans other notebooks and may include inaccessible ancestors.
                        continue
                    metadata = self._metadata(note_id)
                    self._check_location(note_id, self._title(page), metadata, parent_path)
                    if metadata["kind"] == "note":
                        notes.append(NoteSummary(note_id, self._title(page), metadata["path"]))
            if not result.get("has_more"):
                return notes
            cursor = result.get("next_cursor")
            if not cursor or cursor in seen_cursors:
                raise ProviderError("Notion returned an invalid search pagination cursor.")
            seen_cursors.add(cursor)
            payload = {**payload, "start_cursor": cursor}

    def search_content(self, query: str) -> list[NoteSummary]:
        raise UnsupportedCapability("Content search is not supported by the notion backend.")

    def update(self, note_id, body, *, revision=None, unconditional=False):
        validate_write(self.capabilities, revision, unconditional)
        note = self.read(note_id)
        note_id = note.id
        if note.archived:
            raise ShyNoteError("The note is archived.")
        result = self.transport.request("PATCH", f"pages/{note_id}/markdown", {
            "type": "replace_content", "replace_content": {"new_str": notion_metadata.encode(body, note.path)},
        })
        if (result.get("truncated") or result.get("unknown_block_ids")
                or not isinstance(result.get("markdown"), str)):
            raise ProviderError("Notion did not acknowledge complete Markdown after the write; inspect the note before retrying.")
        acknowledged, path = notion_metadata.decode(result["markdown"])
        if path != note.path:
            raise Conflict("Notion did not preserve the note path; inspect the remote note before retrying.")
        return acknowledged

    def archive(self, note_id, *, revision=None, unconditional=False):
        raise UnsupportedCapability("Archive is not supported by the notion backend.")

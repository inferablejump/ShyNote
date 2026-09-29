import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import UUID

from ..config import NotebookConfig, NotionConfig
from ..model import (Capabilities, Conflict, Note, NoteSummary, NotFound,
                     ProviderError, ShyNoteError, UnsupportedCapability,
                     validate_title, validate_write)


class NotionTransport:
    """Direct REST transport. No hosted MCP or model calls."""

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
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            status = exc.code
            retry_after = exc.headers.get("Retry-After")
            exc.close()
            if status == 404:
                raise NotFound("Note not found or not accessible.") from exc
            if status == 409:
                raise Conflict("Notion reported a conflict.") from exc
            suffix = f" Retry after {retry_after} seconds." if status == 429 and retry_after else ""
            raise ProviderError(f"Notion request failed (HTTP {status}).{suffix}") from exc
        except URLError as exc:
            raise ProviderError("Cannot reach Notion. A failed write may have succeeded; inspect before retrying.") from exc


class NotionStore:
    capabilities = Capabilities(conditional_writes=False, title_search=True)

    def __init__(self, config: NotebookConfig, *, transport=None):
        assert isinstance(config.storage, NotionConfig)
        self.settings = config.storage
        self.transport = transport if transport is not None else NotionTransport(self.settings.token_env)

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
        page = self.transport.request("GET", f"pages/{self._id(note_id)}")
        parent = page.get("parent", {})
        if parent.get("type") != "page_id" or self._id(parent.get("page_id", "")) != self.settings.parent_page_id:
            raise NotFound("Note is outside this notebook.")
        return page

    @staticmethod
    def _archived(page):
        return bool(page.get("in_trash") or page.get("is_archived") or page.get("archived"))

    @staticmethod
    def _title(page):
        return "".join(text.get("plain_text", text.get("text", {}).get("content", ""))
                       for prop in page["properties"].values() if prop.get("type") == "title"
                       for text in prop["title"])

    def create(self, title: str, body: str) -> Note:
        validate_title(title)
        page = self.transport.request("POST", "pages", {
            "parent": {"type": "page_id", "page_id": self.settings.parent_page_id},
            "properties": {"title": {"type": "title", "title": [
                {"type": "text", "text": {"content": title}}]}},
            "markdown": body,
        })
        return self.read(page["id"])

    def read(self, note_id: str) -> Note:
        note_id = self._id(note_id)
        for _ in range(3):
            before = self._page(note_id)
            content = self.transport.request("GET", f"pages/{note_id}/markdown")
            after = self._page(note_id)
            if before["last_edited_time"] != after["last_edited_time"]:
                continue
            if content.get("truncated") or content.get("unknown_block_ids"):
                raise ProviderError("Notion returned incomplete Markdown; refusing a partial note.")
            return Note(note_id, self._title(after), content["markdown"], after["last_edited_time"], self._archived(after))
        raise Conflict("The note kept changing while it was being read.")

    def list_notes(self) -> list[NoteSummary]:
        notes = []
        cursor = None
        seen = set()
        while True:
            query = {"page_size": 100}
            if cursor:
                query["start_cursor"] = cursor
            page = self.transport.request("GET", f"blocks/{self.settings.parent_page_id}/children?{urlencode(query)}")
            for block in page["results"]:
                if block["type"] == "child_page" and not self._archived(block):
                    notes.append(NoteSummary(self._id(block["id"]), block["child_page"]["title"]))
            if not page.get("has_more"):
                return notes
            cursor = page.get("next_cursor")
            if not cursor or cursor in seen:
                raise ProviderError("Notion returned an invalid pagination cursor.")
            seen.add(cursor)

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
                if self._id(parent.get("page_id", "")) != self.settings.parent_page_id:
                    continue
                note_id = self._id(page["id"])
                if note_id not in seen_ids:
                    notes.append(NoteSummary(note_id, self._title(page)))
                    seen_ids.add(note_id)
            if not result.get("has_more"):
                return notes
            cursor = result.get("next_cursor")
            if not cursor or cursor in seen_cursors:
                raise ProviderError("Notion returned an invalid search pagination cursor.")
            seen_cursors.add(cursor)
            payload = {**payload, "start_cursor": cursor}

    def search_content(self, query: str) -> list[NoteSummary]:
        raise UnsupportedCapability("Content search is not supported by the notion backend.")

    def _writable(self, note_id, revision, unconditional):
        validate_write(self.capabilities, revision, unconditional)
        note_id = self._id(note_id)
        if self._archived(self._page(note_id)):
            raise ShyNoteError("The note is archived.")
        return note_id

    def update(self, note_id, body, *, revision=None, unconditional=False):
        note_id = self._writable(note_id, revision, unconditional)
        result = self.transport.request("PATCH", f"pages/{note_id}/markdown", {
            "type": "replace_content", "replace_content": {"new_str": body},
        })
        if (result.get("truncated") or result.get("unknown_block_ids")
                or not isinstance(result.get("markdown"), str)):
            raise ProviderError("Notion did not acknowledge complete Markdown after the write; inspect the note before retrying.")
        return result["markdown"]

    def archive(self, note_id, *, revision=None, unconditional=False):
        raise UnsupportedCapability("Archive is not supported by the notion backend.")

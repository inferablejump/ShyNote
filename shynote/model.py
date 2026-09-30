from dataclasses import dataclass
from typing import Protocol


class ShyNoteError(Exception):
    """An actionable notebook error."""


class ConfigurationError(ShyNoteError):
    pass


class NotFound(ShyNoteError):
    pass


class Conflict(ShyNoteError):
    pass


class UnsupportedCapability(ShyNoteError):
    pass


class ProviderError(ShyNoteError):
    pass


class CreatedNoteError(ProviderError):
    """Creation succeeded, but the note could not be verified for local tracking."""

    def __init__(self, note_id: str, message: str):
        super().__init__(message)
        self.note_id = note_id


@dataclass(frozen=True)
class Capabilities:
    conditional_writes: bool
    title_search: bool = False
    content_search: bool = False
    archive: bool = False


@dataclass(frozen=True)
class NoteSummary:
    id: str
    title: str
    path: str
    archived: bool = False


@dataclass(frozen=True)
class Note:
    id: str
    title: str
    body: str
    revision: str
    path: str
    archived: bool = False


class Store(Protocol):
    capabilities: Capabilities

    def check_access(self) -> None: ...
    def create(self, title: str, body: str, *, path: str) -> Note: ...
    def read(self, note_id: str) -> Note: ...
    def list_notes(self) -> list[NoteSummary]: ...
    def search_title(self, query: str) -> list[NoteSummary]: ...
    def search_content(self, query: str) -> list[NoteSummary]: ...
    def update(self, note_id: str, body: str, *, revision: str | None = None,
               unconditional: bool = False) -> str:
        """Return the Markdown acknowledged by the write, including normalization."""
        ...
    def archive(self, note_id: str, *, revision: str | None = None,
                unconditional: bool = False) -> None:
        """Retain content readable by ID while hiding it from active lists/search.

        Raise UnsupportedCapability if the backend cannot provide this behavior.
        """
        ...


def validate_title(title: str) -> None:
    if not title.strip() or len(title) > 200:
        raise ShyNoteError("Note titles must contain 1–200 characters.")


def validate_path(path: str) -> None:
    """Validate portable, canonical paths before trusting remote metadata."""
    if (not isinstance(path, str) or not path or len(path.encode("utf-8")) > 1024
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or "\\" in path or ":" in path
            or any(part in {"", ".", "..", ".git", ".shynote", ".shynote-local"}
                   for part in path.split("/"))):
        raise ShyNoteError("Invalid note path: expected a relative path inside notes_dir, without traversal or metadata directories.")


def validate_write(capabilities: Capabilities, revision: str | None,
                   unconditional: bool) -> None:
    if unconditional and revision is not None:
        raise ShyNoteError("Choose a revision or an unconditional write, not both.")
    if not unconditional and not revision:
        raise ShyNoteError("A revision is required; explicitly opt into an unconditional write otherwise.")
    if revision and not capabilities.conditional_writes:
        raise UnsupportedCapability("This backend has no atomic conditional writes. Use an unconditional write explicitly.")

import base64
from contextlib import contextmanager
from dataclasses import replace
import json
from uuid import UUID, uuid4

from ..config import NotebookConfig, S3Config
from ..model import (Capabilities, Conflict, Note, NoteSummary, NotFound,
                     ProviderError, ShyNoteError, UnsupportedCapability,
                     validate_path, validate_title, validate_write)


class S3Store:
    capabilities = Capabilities(conditional_writes=True, title_search=True, archive=True)

    def __init__(self, config: NotebookConfig, *, client=None):
        assert isinstance(config.storage, S3Config)
        self.settings = config.storage
        self.prefix = f"{self.settings.prefix}/{config.notebook}/notes/"
        self._client = client

    @property
    def client(self):
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config
            except ModuleNotFoundError as exc:
                raise ProviderError("S3 backend dependencies are missing. Install 'shynote[s3]' or run 'uv sync --extra s3'.") from exc
            # Login-session refresh creates its own regional Sign-In client.
            # Supplying a region only to the S3 client does not configure it.
            self._client = boto3.Session(region_name=self.settings.region).client(
                "s3", region_name=self.settings.region,
                endpoint_url=self.settings.endpoint_url,
                config=Config(connect_timeout=5, read_timeout=30,
                              retries={"mode": "standard", "total_max_attempts": 3}),
            )
        return self._client

    def _key(self, note_id: str) -> str:
        try:
            canonical = str(UUID(note_id))
        except ValueError as exc:
            raise NotFound("Invalid note ID.") from exc
        return f"{self.prefix}{canonical}.md"

    @contextmanager
    def _errors(self, operation: str):
        # Translate SDK errors at the provider boundary; never hide auth failures.
        try:
            from botocore.exceptions import BotoCoreError, ClientError
        except ModuleNotFoundError as exc:
            raise ProviderError("S3 backend dependencies are missing. Install 'shynote[s3]' or run 'uv sync --extra s3'.") from exc
        try:
            yield
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in {"NoSuchKey", "404"}:
                raise NotFound("Note not found.") from exc
            if code in {"PreconditionFailed", "ConditionalRequestConflict", "412", "409"}:
                raise Conflict("The remote note changed; read it again before writing.") from exc
            raise ProviderError(f"S3 {operation} failed ({code}).") from exc
        except BotoCoreError as exc:
            raise ProviderError(f"S3 {operation} failed ({type(exc).__name__}). Check credentials and connectivity; inspect before retrying writes.") from exc

    def _call(self, operation: str, **kwargs):
        with self._errors(operation):
            return getattr(self.client, operation)(Bucket=self.settings.bucket, **kwargs)

    def _pages(self):
        with self._errors("list_objects_v2"):
            paginator = self.client.get_paginator("list_objects_v2")
            yield from paginator.paginate(Bucket=self.settings.bucket, Prefix=self.prefix)

    def check_access(self):
        self._call("list_objects_v2", Prefix=self.prefix, MaxKeys=1)

    def _put(self, note: Note, **condition) -> str:
        header = {"version": 1, "id": note.id, "title": note.title, "archived": note.archived}
        metadata = {"shynote-title": base64.b64encode(note.title.encode()).decode(),
                    "shynote-archived": str(note.archived).lower()}
        validate_path(note.path)
        header["path"] = note.path
        metadata["shynote-path"] = base64.b64encode(note.path.encode()).decode()
        payload = f"---\n{json.dumps(header, ensure_ascii=False)}\n---\n{note.body}"
        result = self._call(
            "put_object", Key=self._key(note.id), Body=payload.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
            Metadata=metadata,
            **condition,
        )
        return result["ETag"]

    def create(self, title: str, body: str, *, path: str) -> Note:
        validate_title(title)
        note = Note(str(uuid4()), title, body, "", path=path)
        return replace(note, revision=self._put(note, IfNoneMatch="*"))

    def read(self, note_id: str) -> Note:
        result = self._call("get_object", Key=self._key(note_id))
        with result["Body"] as stream:
            payload = stream.read().decode("utf-8")
        try:
            start, encoded, body = payload.split("\n", 2)
            if start != "---" or not body.startswith("---\n"):
                raise ValueError("Missing frontmatter")
            header = json.loads(encoded)
            if header["version"] != 1 or header["id"] != str(UUID(note_id)):
                raise ValueError("Invalid note identity")
            if not isinstance(header["title"], str) or type(header["archived"]) is not bool:
                raise ValueError("Invalid note metadata")
            path = header["path"]
            validate_path(path)
            return Note(header["id"], header["title"], body[4:], result["ETag"], path, header["archived"])
        except (KeyError, TypeError, ValueError, ShyNoteError) as exc:
            raise ProviderError(f"Object {note_id} is not a valid ShyNote document; a valid saved relative path is required.") from exc

    def list_notes(self) -> list[NoteSummary]:
        notes = []
        for page in self._pages():
            for item in page.get("Contents", []):
                key = item["Key"]
                suffix = key.removeprefix(self.prefix)
                if not key.startswith(self.prefix) or not suffix.endswith(".md"):
                    continue
                note_id = suffix[:-3]
                try:
                    if self._key(note_id) != key:
                        continue
                except NotFound:
                    continue
                try:
                    metadata = self._call("head_object", Key=key)["Metadata"]
                except NotFound:
                    continue  # A note may disappear between LIST and HEAD.
                if metadata.get("shynote-archived") == "true":
                    continue
                try:
                    title = base64.b64decode(metadata["shynote-title"], validate=True).decode()
                    path = base64.b64decode(metadata["shynote-path"], validate=True).decode()
                    validate_path(path)
                except (KeyError, ValueError, UnicodeError, ShyNoteError) as exc:
                    raise ProviderError(f"Invalid ShyNote object metadata for {note_id}; a saved relative path is required.") from exc
                notes.append(NoteSummary(note_id, title, path=path))
        return notes

    def _change(self, note_id, *, body=None, archive=False, revision=None, unconditional=False):
        validate_write(self.capabilities, revision, unconditional)
        note = self.read(note_id)
        if revision and revision != note.revision:
            raise Conflict("The remote note changed; read it again before writing.")
        if note.archived:
            raise ShyNoteError("The note is archived.")
        updated = replace(note, body=note.body if body is None else body, archived=archive)
        # Even explicitly unconditional updates protect the snapshot just read.
        self._put(updated, IfMatch=note.revision)

    def search_title(self, query: str) -> list[NoteSummary]:
        if not isinstance(query, str) or not query.strip():
            raise ShyNoteError("Title search requires a nonempty query. Use list to browse notes.")
        needle = query.strip().casefold()
        return [note for note in self.list_notes() if needle in note.title.casefold()]

    def search_content(self, query: str) -> list[NoteSummary]:
        raise UnsupportedCapability("Content search is not supported by the s3 backend.")

    def update(self, note_id, body, *, revision=None, unconditional=False):
        self._change(note_id, body=body, revision=revision, unconditional=unconditional)
        return body

    def archive(self, note_id, *, revision=None, unconditional=False):
        self._change(note_id, archive=True, revision=revision, unconditional=unconditional)

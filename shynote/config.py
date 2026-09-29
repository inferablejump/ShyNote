from dataclasses import dataclass
from pathlib import Path
import re
import tomllib
from uuid import UUID
from urllib.parse import urlsplit

from .model import ConfigurationError


@dataclass(frozen=True)
class S3Config:
    bucket: str
    prefix: str
    region: str | None = None
    endpoint_url: str | None = None


@dataclass(frozen=True)
class NotionConfig:
    parent_page_id: str
    token_env: str = "NOTION_TOKEN"


@dataclass(frozen=True)
class NotebookConfig:
    root: Path
    notebook: str
    backend: str
    storage: S3Config | NotionConfig
    notes_dir: str


def validate_notes_dir(root: Path, value: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(c in value for c in "\r\n\x00"):
        raise ConfigurationError("notes_dir must be a nonempty relative directory path.")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ConfigurationError("notes_dir must stay inside the repository.")
    if any(part in {".git", ".shynote", ".shynote-local"} for part in path.parts):
        raise ConfigurationError("notes_dir cannot use repository or ShyNote metadata paths.")
    current = root
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise ConfigurationError("notes_dir must not contain symbolic links.")
        if current.exists() and not current.is_dir():
            raise ConfigurationError("notes_dir must be a directory or a path that can be created.")
    return path.as_posix()


def _keys(data: dict, allowed: set[str], required: set[str], label: str) -> None:
    unknown = set(data) - allowed
    missing = required - set(data)
    if unknown or missing:
        raise ConfigurationError(f"Invalid {label} fields; unknown={sorted(unknown)}, missing={sorted(missing)}")


def _text(data: dict, key: str) -> str:
    value = data[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{key} must be a nonempty string.")
    return value


def load_config(start: Path | str = ".") -> NotebookConfig:
    root = Path(start).resolve()
    if not root.is_dir():
        raise ConfigurationError("Repository path must be a directory.")
    for candidate in (root, *root.parents):
        marker = candidate / ".shynote"
        if marker.exists():
            break
    else:
        raise ConfigurationError("No .shynote marker found in this directory or its parents.")
    try:
        with marker.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"Cannot read {marker}: {exc}") from exc
    return parse_config(data, candidate)


def parse_config(data: dict, root: Path) -> NotebookConfig:
    """Validate a candidate config without writing a marker or contacting storage."""
    _keys(data, {"version", "notebook", "backend", "storage", "notes_dir"},
          {"version", "notebook", "backend", "storage", "notes_dir"}, "notebook")
    if type(data["version"]) is not int or data["version"] != 1:
        raise ConfigurationError("Only .shynote version 1 is supported.")
    notebook = _text(data, "notebook")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", notebook):
        raise ConfigurationError("notebook must contain only letters, digits, underscores, and hyphens.")
    backend = _text(data, "backend")
    storage = data["storage"]
    if not isinstance(storage, dict):
        raise ConfigurationError("storage must be one table.")
    if backend == "s3":
        _keys(storage, {"bucket", "prefix", "region", "endpoint_url"}, {"bucket", "prefix"}, "S3 storage")
        bucket = _text(storage, "bucket")
        if "/" in bucket or any(c.isspace() for c in bucket):
            raise ConfigurationError("bucket must be a bucket name.")
        prefix = _text(storage, "prefix").strip("/")
        if not prefix or any(part in {".", "..", ""} for part in prefix.split("/")):
            raise ConfigurationError("prefix must be a nonempty relative object prefix.")
        region = _text(storage, "region") if "region" in storage else None
        endpoint = _text(storage, "endpoint_url") if "endpoint_url" in storage else None
        if endpoint:
            url = urlsplit(endpoint)
            if url.username or url.password or url.query or url.fragment or not url.hostname:
                raise ConfigurationError("endpoint_url must not contain credentials, query, or fragment.")
            if url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}):
                raise ConfigurationError("endpoint_url must use HTTPS (HTTP is allowed for loopback tests).")
        settings = S3Config(bucket, prefix, region, endpoint)
    elif backend == "notion":
        _keys(storage, {"parent_page_id", "token_env"}, {"parent_page_id"}, "Notion storage")
        try:
            page_id = str(UUID(_text(storage, "parent_page_id")))
        except ValueError as exc:
            raise ConfigurationError("parent_page_id must be a UUID.") from exc
        token_env = _text(storage, "token_env") if "token_env" in storage else "NOTION_TOKEN"
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
            raise ConfigurationError("token_env must name an environment variable.")
        settings = NotionConfig(page_id, token_env)
    else:
        raise ConfigurationError("backend must be exactly one of 'notion' or 's3'.")
    notes_dir = validate_notes_dir(root, data["notes_dir"])
    return NotebookConfig(root, notebook, backend, settings, notes_dir)

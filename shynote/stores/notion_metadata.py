"""A required first code block stores the path independently of the page title."""
import json
import re

from ..model import ProviderError, validate_path

MARKER = "shynote-metadata"
_BLOCK = re.compile(r"\A```[^\n]*\n(shynote-metadata\n.*?)\n```(?:\n\n|\n|$)", re.DOTALL)


def decode_metadata(content):
    try:
        marker, encoded = content.split("\n", 1)
        metadata = json.loads(encoded)
        if marker != MARKER or type(metadata["version"]) is not int or metadata["version"] != 1:
            raise ValueError("Invalid metadata version")
        if metadata["kind"] not in ("note", "directory"):
            raise ValueError("Invalid page kind")
        path = metadata["path"]
        validate_path(path)
        return metadata
    except (KeyError, ValueError, TypeError) as exc:
        raise ProviderError("Missing or invalid ShyNote path metadata. A first shynote-metadata code block with version 1, kind (note or directory), and a relative path is required.") from exc


def encode(body, path, *, kind="note"):
    validate_path(path)
    if kind not in ("note", "directory"):
        raise ProviderError("Invalid ShyNote page kind.")
    metadata = json.dumps({"version": 1, "kind": kind, "path": path}, ensure_ascii=False)
    return f"```text\n{MARKER}\n{metadata}\n```\n\n{body}"


def decode(markdown):
    block = _BLOCK.match(markdown)
    if block is None:
        raise ProviderError("Missing ShyNote path metadata: the page must start with a shynote-metadata code block. No path will be inferred from its title.")
    metadata = decode_metadata(block[1])
    if metadata["kind"] != "note":
        raise ProviderError("This ShyNote page is a directory, not a note.")
    return markdown[block.end():], metadata["path"]

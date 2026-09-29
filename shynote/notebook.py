from dataclasses import dataclass
from pathlib import Path

from .config import NotebookConfig, load_config
from .model import NoteSummary, Store


@dataclass
class Notebook:
    config: NotebookConfig
    store: Store

    def search_title(self, query: str) -> list[NoteSummary]:
        return self.store.search_title(query)

    def search_content(self, query: str) -> list[NoteSummary]:
        return self.store.search_content(query)


def open_notebook(path: Path | str = ".", *, s3_client=None, notion_transport=None) -> Notebook:
    """Resolve one backend, without network calls or credential lookup."""
    config = load_config(path)
    return notebook_from_config(config, s3_client=s3_client, notion_transport=notion_transport)


def notebook_from_config(config: NotebookConfig, *, s3_client=None, notion_transport=None) -> Notebook:
    """Open a validated configuration, including one not yet saved by init."""
    if config.backend == "s3":
        from .stores.s3 import S3Store
        store = S3Store(config, client=s3_client)
    else:
        from .stores.notion import NotionStore
        store = NotionStore(config, transport=notion_transport)
    return Notebook(config, store)

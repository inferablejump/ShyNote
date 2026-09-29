"""Opt-in smoke test: creates/updates notes, archiving only where supported."""
import argparse
from uuid import uuid4

from shynote.notebook import open_notebook
from shynote.model import UnsupportedCapability


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", action="append", required=True)
    args = parser.parse_args()
    notebooks = [open_notebook(path) for path in args.repo]
    if len(notebooks) != 2 or {n.config.backend for n in notebooks} != {"s3", "notion"}:
        parser.error("Supply exactly two disposable repositories: one S3 and one Notion.")
    for notebook in notebooks:
        store = notebook.store
        title = f"ShyNote smoke {uuid4()}"
        note = store.create(title, "# Smoke test\n\nA disposable ShyNote test note.", path="note.md")
        print(f"{notebook.config.backend}: created {note.id}", flush=True)
        try:
            fetched = store.read(note.id)
            assert fetched.title == title
            assert "disposable ShyNote" in fetched.body
            assert note.id in {item.id for item in store.list_notes()}
            guard = {"revision": fetched.revision} if store.capabilities.conditional_writes else {"unconditional": True}
            store.update(note.id, "Updated smoke test.", **guard)
            assert "Updated smoke test." in store.read(note.id).body
        finally:
            if store.capabilities.archive:
                current = store.read(note.id)
                guard = {"revision": current.revision} if store.capabilities.conditional_writes else {"unconditional": True}
                store.archive(note.id, **guard)
                print(f"{notebook.config.backend}: archived {note.id}", flush=True)
            else:
                print(f"{notebook.config.backend}: retained {note.id}; archive unsupported; remove manually if desired", flush=True)
        if store.capabilities.archive:
            assert note.id not in {item.id for item in store.list_notes()}
            assert "Updated smoke test." in store.read(note.id).body
        else:
            try:
                store.archive(note.id, unconditional=True)
            except UnsupportedCapability:
                pass
            else:
                raise AssertionError("Backend accepted an unsupported archive")
            assert note.id in {item.id for item in store.list_notes()}
            assert "Updated smoke test." in store.read(note.id).body
        print(f"{notebook.config.backend}: passed", flush=True)


if __name__ == "__main__":
    main()

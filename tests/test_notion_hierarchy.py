from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from shynote.model import NotFound, ProviderError, ShyNoteError
from shynote.notebook import Notebook, open_notebook
from shynote.stores.notion import NotionStore
from shynote.working_copy import WorkingCopy
from tests.fakes import FakeNotion

FIXTURE = Path(__file__).parent / "fixtures/notion-repo"


class NotionHierarchyTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeNotion()
        self.notebook = open_notebook(FIXTURE, notion_transport=self.transport)
        self.store = self.notebook.store
        self.root = self.store.settings.parent_page_id

    def parent(self, page_id):
        return self.transport.pages[page_id]["parent"]["page_id"]

    def title(self, page_id, title):
        self.transport.pages[page_id]["properties"]["title"]["title"][0]["text"]["content"] = title

    def move(self, page_id, parent_id):
        self.transport.pages[page_id]["parent"]["page_id"] = parent_id
        self.transport.pages[page_id]["last_edited_time"] = self.transport.revision()

    def clone(self, page_id):
        clone_id = str(uuid4())
        self.transport.pages[clone_id] = {**deepcopy(self.transport.pages[page_id]), "id": clone_id}
        self.transport.bodies[clone_id] = self.transport.bodies[page_id]
        return clone_id

    def assert_no_writes(self):
        self.assertFalse(any(method == "PATCH" or (method == "POST" and route == "pages")
                             for method, route, _ in self.transport.calls))

    def test_create_mirrors_directories_and_reuses_them_across_store_instances(self):
        first = self.store.create("Authentication", "first", path="design/日本語/auth.md")
        inner = self.parent(first.id)
        outer = self.parent(inner)
        self.assertEqual(self.parent(outer), self.root)
        self.assertEqual(self.store._metadata(outer), {"version": 1, "kind": "directory", "path": "design"})
        self.assertEqual(self.store._metadata(inner)["path"], "design/日本語")
        self.assertEqual(self.store._title(self.transport.pages[inner]), "日本語")
        reopened = NotionStore(self.notebook.config, transport=self.transport)
        second = reopened.create("Caching", "second", path="design/日本語/cache.md")
        other = reopened.create("Authentication", "third", path="research/auth.md")
        self.assertEqual(self.parent(second.id), inner)
        self.assertNotEqual(self.parent(other.id), inner)
        self.assertEqual(len(self.transport.pages), 6)  # Three directories and three notes.
        self.assertEqual(reopened.read(first.id).path, "design/日本語/auth.md")

    def test_paginated_listing_and_search_return_nested_notes_without_directories_or_bodies(self):
        first = self.store.create("Cache", "a", path="Cache/deep/a.md")
        second = self.store.create("Cache", "b", path="other/b.md")
        root_note = self.store.create("Cache", "c", path="c.md")
        # Ordinary content on the notebook root does not make it a note or a directory.
        self.transport.bodies[self.root] = "Notebook introduction"
        self.transport.calls.clear()
        listing = self.store.list_notes()
        matches = self.store.search_title("Cache")
        self.assertEqual({note.id for note in listing}, {first.id, second.id, root_note.id})
        self.assertEqual({note.id for note in matches}, {first.id, second.id, root_note.id})
        self.assertTrue(any("start_cursor=" in route for _, route, _ in self.transport.calls))
        self.assertFalse(any("/markdown" in route for _, route, _ in self.transport.calls))
        self.assert_no_writes()

    def test_note_update_keeps_directory_contents_intact_and_directory_is_not_writable(self):
        note = self.store.create("Title", "original", path="design/note.md")
        folder = self.parent(note.id)
        original = self.transport.bodies[folder]
        self.transport.calls.clear()
        self.store.update(note.id, "updated", unconditional=True)
        self.assertEqual(self.transport.bodies[folder], original)
        self.assertEqual(self.parent(note.id), folder)
        self.assertEqual(self.store.read(note.id).body, "updated")
        for operation in (lambda: self.store.read(folder),
                          lambda: self.store.update(folder, "overwrite", unconditional=True)):
            self.transport.calls.clear()
            with self.assertRaisesRegex(ProviderError, "directory, not a note"):
                operation()
            self.assert_no_writes()

    def test_manual_note_move_or_directory_rename_fails_without_writes(self):
        note = self.store.create("Title", "original", path="design/note.md")
        folder = self.parent(note.id)
        other = self.store.create("Other", "body", path="research/other.md")
        for change in (lambda: self.move(note.id, self.parent(other.id)),
                       lambda: self.title(folder, "renamed")):
            change()
            for operation in (lambda: self.store.read(note.id), self.store.list_notes,
                              lambda: self.store.search_title("Title"),
                              lambda: self.store.update(note.id, "overwrite", unconditional=True)):
                self.transport.calls.clear()
                with self.assertRaisesRegex(ProviderError, "hierarchy does not match"):
                    operation()
                self.assert_no_writes()
            self.move(note.id, folder)
        self.title(folder, "design")
        self.title(note.id, "New display title")
        self.assertEqual(self.store.read(note.id).path, "design/note.md")
        self.assertEqual(self.store.read(note.id).title, "New display title")

    def test_outside_and_archived_subtrees_are_not_notebook_notes(self):
        note = self.store.create("Title", "body", path="design/note.md")
        folder = self.parent(note.id)
        for change in (lambda: self.move(folder, str(uuid4())),
                       lambda: self.transport.pages[folder].update(in_trash=True)):
            change()
            self.transport.calls.clear()
            self.assertEqual(self.store.list_notes(), [])
            self.assertEqual(self.store.search_title("Title"), [])
            with self.assertRaises(NotFound):
                self.store.read(note.id)
            with self.assertRaises(NotFound):
                self.store.update(note.id, "overwrite", unconditional=True)
            self.assert_no_writes()
            self.move(folder, self.root)

    def test_notes_cannot_be_used_as_directory_pages(self):
        note = self.store.create("design", "original", path="design")
        self.transport.calls.clear()
        with self.assertRaisesRegex(ProviderError, "occupied by a note"):
            self.store.create("Nested", "body", path="design/note.md")
        self.assert_no_writes()
        nested = self.store.create("Title", "body", path="actual/note.md")
        self.move(nested.id, note.id)
        self.transport.calls.clear()
        with self.assertRaisesRegex(ProviderError, "not a ShyNote directory"):
            self.store.read(nested.id)
        self.assert_no_writes()

    def test_directory_path_cannot_be_used_for_a_note(self):
        self.store.create("Nested", "body", path="design/note.md")
        self.transport.calls.clear()
        with self.assertRaisesRegex(ProviderError, "occupied by a directory"):
            self.store.create("Title", "body", path="design")
        self.assert_no_writes()

    def test_duplicate_directory_paths_fail_instead_of_choosing_one(self):
        note = self.store.create("Title", "body", path="design/note.md")
        self.clone(self.parent(note.id))
        for operation in (self.store.list_notes,
                          lambda: self.store.create("Another", "body", path="design/another.md")):
            self.transport.calls.clear()
            with self.assertRaisesRegex(ProviderError, "Ambiguous Notion directory"):
                operation()
            self.assert_no_writes()

    def test_missing_metadata_or_kind_is_never_inferred_from_a_directory_title(self):
        note = self.store.create("Title", "body", path="design/note.md")
        folder = self.parent(note.id)
        for body in ("Ordinary page", '```text\nshynote-metadata\n{"version":1,"path":"design"}\n```'):
            self.transport.bodies[folder] = body
            for operation in (self.store.list_notes, lambda: self.store.read(note.id),
                              lambda: self.store.search_title("Title"),
                              lambda: self.store.create("Another", "body", path="design/another.md")):
                self.transport.calls.clear()
                with self.assertRaisesRegex(ProviderError, "metadata"):
                    operation()
                self.assert_no_writes()

    def test_directory_creation_failure_leaves_earlier_directory_reusable(self):
        request = self.transport.request

        def fail_second_directory(method, route, payload=None):
            if method == "POST" and route == "pages" and payload["properties"]["title"]["title"][0]["text"]["content"] == "deep":
                raise ProviderError("simulated write failure")
            return request(method, route, payload)

        with patch.object(self.transport, "request", side_effect=fail_second_directory):
            with self.assertRaisesRegex(ProviderError, "simulated"):
                self.store.create("Title", "body", path="design/deep/note.md")
        self.assertEqual(len(self.transport.pages), 1)
        folder = next(iter(self.transport.pages))
        note = self.store.create("Title", "body", path="design/deep/note.md")
        self.assertEqual(len(self.transport.pages), 3)
        self.assertEqual(self.parent(self.parent(note.id)), folder)

    def test_dry_run_creates_no_directories_and_invalid_names_fail_before_writes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "new/nested").mkdir(parents=True)
            (root / "new/nested/note.md").write_text("draft")
            notebook = Notebook(replace(self.notebook.config, root=root, notes_dir="."), self.store)
            result = WorkingCopy(notebook).transfer("push", "new/nested/note.md", dry_run=True)
            self.assertEqual(result["results"][0]["status"], "would_create")
            self.assertEqual(self.transport.pages, {})
        self.transport.calls.clear()
        with self.assertRaises(ShyNoteError):
            self.store.create("Title", "body", path="valid/" + "x" * 201 + "/note.md")
        self.assert_no_writes()

    def test_pagination_and_ancestry_cycles_fail(self):
        with patch.object(self.transport, "request", return_value={"results": [], "has_more": True, "next_cursor": "repeat"}):
            with self.assertRaisesRegex(ProviderError, "pagination cursor"):
                self.store.list_notes()
        note = self.store.create("Title", "body", path="a/b/note.md")
        inner = self.parent(note.id)
        outer = self.parent(inner)
        self.move(outer, inner)
        with self.assertRaisesRegex(ProviderError, "cycle"):
            self.store.read(note.id)

    def test_bulk_push_discovers_each_directory_once_and_discards_cache(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "implementation").mkdir()
            for number in range(30):
                (root / f"implementation/note-{number:02}.md").write_text(f"body {number}")
            notebook = Notebook(replace(self.notebook.config, root=root, notes_dir="."), self.store)
            results = WorkingCopy(notebook).transfer("push", all_files=True)["results"]
            self.assertTrue(all(result["status"] == "created" for result in results))
            self.assertEqual(len(results), 30)
            discovery_calls = [route for method, route, _ in self.transport.calls
                               if method == "GET" and "page_size=100" in route]
            self.assertEqual(len(discovery_calls), 2)  # Root and implementation, once each.
            self.assertIsNone(self.store._discovery_cache)
            self.assertEqual(len(self.store.list_notes()), 30)

    def test_cached_directory_location_is_checked_before_reuse_and_cache_clears_on_error(self):
        note = self.store.create("Title", "body", path="design/note.md")
        folder = self.parent(note.id)
        with self.assertRaises(NotFound):
            with self.store.transfer_batch():
                self.store.create("Second", "body", path="design/second.md")
                self.move(folder, str(uuid4()))
                self.transport.calls.clear()
                self.store.create("Third", "body", path="design/third.md")
        self.assert_no_writes()
        self.assertIsNone(self.store._discovery_cache)


if __name__ == "__main__":
    unittest.main()

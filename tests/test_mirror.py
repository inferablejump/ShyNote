from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from shynote.cli import main
from shynote.model import Conflict, ProviderError, ShyNoteError
from shynote.notebook import Notebook, open_notebook
from shynote.working_copy import WorkingCopy
from tests.fakes import FakeNotion, FakeS3


class MirrorTests(unittest.TestCase):
    @contextmanager
    def notebook(self, backend="s3"):
        with TemporaryDirectory() as directory:
            fixture = Path(__file__).parent / "fixtures" / f"{backend}-repo"
            notebook = open_notebook(fixture, s3_client=FakeS3(), notion_transport=FakeNotion())
            config = replace(notebook.config, root=Path(directory), notes_dir=".agent/notes")
            notebook = Notebook(config, notebook.store)
            yield notebook, WorkingCopy(notebook)

    def draft(self, copy, name="draft.md", body="unpublished\n"):
        path = copy.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def test_rebuild_missing_corrupt_or_mismatched_state_on_both_backends(self):
        for backend in ("s3", "notion"):
            for old_state in (None, b"broken JSON", b'{"version":1,"identity":{},"files":{}}'):
                with self.subTest(backend=backend, old_state=old_state), self.notebook(backend) as (notebook, copy):
                    notes = [notebook.store.create("Remote", "# café\n", path=name)
                             for name in ("README.md", "nested/note.md")]
                    self.draft(copy, "README.md", "different local content")
                    self.draft(copy)
                    (copy.root / "empty").mkdir()
                    (copy.root / "binary.bin").write_bytes(b"\xff")
                    outside = copy.repo_root / "source.py"
                    outside.write_text("keep")
                    if old_state is not None:
                        copy.directory.mkdir()
                        copy.state_path.write_bytes(old_state)
                    result = copy.transfer("pull", mirror=True, show_diff=True)
                    self.assertTrue(result["mirror"])
                    self.assertEqual({p.relative_to(copy.root).as_posix() for p in copy.root.rglob("*") if p.is_file()},
                                     {n.path for n in notes})
                    for note in notes:
                        self.assertEqual((copy.root / note.path).read_text(), note.body)
                    self.assertFalse((copy.root / "empty").exists())
                    self.assertEqual(outside.read_text(), "keep")
                    state = json.loads(copy.state_path.read_text())
                    self.assertEqual(state["identity"], copy.identity)
                    self.assertEqual({v["id"] for v in state["files"].values()}, {n.id for n in notes})
                    pushed = WorkingCopy(notebook).transfer("push", all_files=True, unconditional=True)
                    self.assertEqual([r["status"] for r in pushed["results"]], ["unchanged", "unchanged"])
                    self.assertEqual(len(notebook.store.list_notes()), 2)

    def test_fresh_checkout_and_empty_upstream(self):
        for backend in ("s3", "notion"):
            with self.subTest(backend=backend), self.notebook(backend) as (notebook, copy):
                note = notebook.store.create("Note", "remote", path="deep/note.md")
                copy.transfer("pull", mirror=True)
                self.assertEqual((copy.root / note.path).read_text(), note.body)
                with patch.object(notebook.store, "list_notes", return_value=[]):
                    copy.transfer("pull", mirror=True)
                self.assertEqual(list(copy.root.iterdir()), [])
                self.assertEqual(json.loads(copy.state_path.read_text())["files"], {})

    def test_dry_run_shows_overwrites_and_removals_without_any_local_writes(self):
        with self.notebook() as (notebook, copy):
            notebook.store.create("Note", "remote\n", path="note.md")
            self.draft(copy, "note.md", "local\n")
            draft = self.draft(copy)
            result = copy.transfer("pull", mirror=True, dry_run=True, show_diff=True)
            by_name = {r["file"]: r for r in result["results"]}
            self.assertEqual(by_name["note.md"]["status"], "would_pull")
            self.assertIn("-local\n+remote\n", by_name["note.md"]["diff"])
            self.assertEqual(by_name["draft.md"]["status"], "would_remove")
            self.assertIn("-unpublished", by_name["draft.md"]["diff"])
            self.assertTrue(draft.exists())
            self.assertEqual((copy.root / "note.md").read_text(), "local\n")
            self.assertFalse(copy.directory.exists())
            self.assertEqual(list(copy.root.parent.iterdir()), [copy.root])

    def test_failed_download_or_listing_preserves_all_files_and_state(self):
        for backend in ("s3", "notion"):
            with self.subTest(backend=backend), self.notebook(backend) as (notebook, copy):
                first = notebook.store.create("A", "remote", path="a.md")
                notebook.store.create("B", "remote", path="b.md")
                draft = self.draft(copy)
                copy.directory.mkdir()
                copy.state_path.write_bytes(b"old state")
                for method, effect in (("read", [first, ProviderError("download failed")]),
                                       ("list_notes", ProviderError("listing failed"))):
                    with patch.object(notebook.store, method, side_effect=effect):
                        with self.assertRaises(ProviderError):
                            copy.transfer("pull", mirror=True)
                    self.assertEqual(list(copy.root.iterdir()), [draft])
                    self.assertEqual(draft.read_text(), "unpublished\n")
                    self.assertEqual(copy.state_path.read_bytes(), b"old state")

    def test_rejects_duplicate_paths_and_file_directory_collisions(self):
        for paths in (("same.md", "same.md"), ("folder", "folder/note.md")):
            with self.subTest(paths=paths), self.notebook() as (notebook, copy):
                for name in paths:
                    notebook.store.create("Note", "remote", path=name)
                draft = self.draft(copy)
                with self.assertRaises(ShyNoteError):
                    copy.transfer("pull", mirror=True)
                self.assertEqual(draft.read_text(), "unpublished\n")
                self.assertFalse(copy.state_path.exists())

    def test_changes_during_download_abort_before_replacement(self):
        for change in ("membership", "path", "archived", "local", "state"):
            with self.subTest(change=change), self.notebook() as (notebook, copy):
                note = notebook.store.create("Note", "remote", path="note.md")
                draft = self.draft(copy)
                listing = notebook.store.list_notes()
                def read(_):
                    if change == "local":
                        draft.write_text("edited during download")
                    if change == "state":
                        copy.state_path.write_bytes(b"another state")
                    return replace(note, path="different.md") if change == "path" else replace(note, archived=True) if change == "archived" else note
                with patch.object(notebook.store, "read", side_effect=read), patch.object(
                    notebook.store, "list_notes", side_effect=[listing, [] if change == "membership" else listing]
                ):
                    with self.assertRaises(Conflict):
                        copy.transfer("pull", mirror=True)
                self.assertTrue(draft.exists())
                self.assertFalse((copy.root / "note.md").exists())

    def test_file_directory_swaps(self):
        with self.notebook() as (notebook, copy):
            self.draft(copy, "folder", "was a file")
            self.draft(copy, "file.md/child.md", "was a directory")
            notebook.store.create("Nested", "nested", path="folder/note.md")
            notebook.store.create("File", "file", path="file.md")
            copy.transfer("pull", mirror=True)
            self.assertEqual((copy.root / "folder/note.md").read_text(), "nested")
            self.assertEqual((copy.root / "file.md").read_text(), "file")

    def test_state_write_failure_restores_original_files_and_tracking(self):
        for old_state in (None, b"old state"):
            with self.subTest(old_state=old_state), self.notebook() as (notebook, copy):
                notebook.store.create("Note", "remote", path="note.md")
                draft = self.draft(copy)
                if old_state is not None:
                    copy.directory.mkdir()
                    copy.state_path.write_bytes(old_state)
                def failed_write(path, content):
                    path.write_text(content)
                    raise OSError("disk full")
                with patch("shynote.mirror._atomic_write", side_effect=failed_write):
                    with self.assertRaisesRegex(OSError, "disk full"):
                        copy.transfer("pull", mirror=True)
                self.assertEqual(list(copy.root.iterdir()), [draft])
                self.assertEqual(draft.read_text(), "unpublished\n")
                self.assertEqual(copy.state_path.read_bytes() if copy.state_path.exists() else None, old_state)
                self.assertEqual(list(copy.root.parent.iterdir()), [copy.root])

    def test_rollback_failure_retains_original_tree(self):
        with self.notebook() as (notebook, copy):
            notebook.store.create("Note", "remote", path="note.md")
            self.draft(copy)
            original_replace = os.replace
            def replace_path(source, destination):
                if Path(source).name == "previous-notes":
                    raise OSError("rollback failed")
                return original_replace(source, destination)
            with patch("shynote.mirror._atomic_write", side_effect=OSError("disk full")), patch(
                "shynote.mirror.os.replace", side_effect=replace_path
            ):
                with self.assertRaisesRegex(ShyNoteError, "Recovery files retained"):
                    copy.transfer("pull", mirror=True)
            backup = next(copy.root.parent.glob(".shynote-mirror-*/previous-notes/draft.md"))
            self.assertEqual(backup.read_text(), "unpublished\n")

    def test_refuses_repo_root_metadata_and_symlinks(self):
        for entry in ("repo_root", ".git", "symlink"):
            with self.subTest(entry=entry), self.notebook() as (notebook, copy):
                draft = self.draft(copy)
                if entry == "repo_root":
                    copy = WorkingCopy(Notebook(replace(notebook.config, notes_dir="."), notebook.store))
                elif entry == ".git":
                    (copy.root / ".git").mkdir()
                else:
                    (copy.root / "link").symlink_to(copy.repo_root)
                with self.assertRaises(ShyNoteError):
                    copy.transfer("pull", mirror=True)
                self.assertTrue(draft.exists())

    def test_cli_mirror_and_invalid_combinations(self):
        with self.notebook() as (notebook, copy):
            notebook.store.create("Note", "remote", path="note.md")
            with patch("shynote.cli.open_notebook", return_value=notebook), redirect_stdout(StringIO()) as output:
                self.assertEqual(main(["pull", "--mirror", "--dry-run"]), 0)
                self.assertTrue(json.loads(output.getvalue())["mirror"])
                self.assertFalse(copy.root.exists())
            for args in (["pull", "--mirror", "--all"], ["pull", "--mirror", "note.md"],
                         ["pull", "--mirror", "--id", "id"], ["push", "--mirror"]):
                with patch("shynote.cli.open_notebook") as opened, redirect_stderr(StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        main(args)
                    self.assertEqual(error.exception.code, 2)
                    opened.assert_not_called()

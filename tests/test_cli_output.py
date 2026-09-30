from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from shynote.cli import main
from shynote.model import CreatedNoteError
from shynote.notebook import open_notebook
from shynote.working_copy import WorkingCopy
from tests.fakes import FakeNotion, FakeS3


class CLIOutputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        fixture = Path(__file__).parent / "fixtures/s3-repo/.shynote"
        (self.root / ".shynote").write_bytes(fixture.read_bytes())
        self.notebook = open_notebook(self.root, s3_client=FakeS3(), notion_transport=FakeNotion())
        self.copy = WorkingCopy(self.notebook)
        self.opened = patch("shynote.cli.open_notebook", return_value=self.notebook)
        self.opened.start()
        self.addCleanup(self.opened.stop)

    def run_cli(self, *arguments, expected=0):
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main(list(arguments)), expected)
        return json.loads(output.getvalue())

    def test_bulk_success_empty_and_verbose(self):
        self.assertEqual(self.run_cli("push", "--all"),
                         {"operation": "push", "dry_run": False, "processed": 0})
        for name in ("a.md", "b.md"):
            (self.root / name).write_text("body")
        self.assertEqual(self.run_cli("push", "a.md", "b.md"),
                         {"operation": "push", "dry_run": False, "processed": 2, "created": 2})
        self.assertEqual(self.run_cli("push", "--all"),
                         {"operation": "push", "dry_run": False, "processed": 2, "unchanged": 2})
        detailed = self.run_cli("push", "--all", "--verbose")
        self.assertEqual(detailed["unchanged"], 2)
        self.assertEqual([r["file"] for r in detailed["results"]], ["a.md", "b.md"])
        self.assertTrue(all(r["id"] and "diff" not in r for r in detailed["results"]))

    def test_dry_run_lists_changed_paths_without_computing_diffs(self):
        (self.root / "a.md").write_text("unchanged")
        self.copy.transfer("push", "a.md")
        (self.root / "b.md").write_text("a very large body\n" * 1000)
        with patch("shynote.working_copy._diff", side_effect=AssertionError("diff not requested")):
            result = self.run_cli("push", "--all", "--dry-run")
        self.assertEqual(result["results"], [{"file": "b.md", "status": "would_create"}])
        self.assertEqual(result["unchanged"], 1)
        self.assertEqual(result["would_create"], 1)
        self.assertEqual(len(self.notebook.store.list_notes()), 1)
        detailed = self.run_cli("push", "--all", "--dry-run", "--verbose")
        self.assertEqual(len(detailed["results"]), 2)
        self.assertTrue(all("diff" not in r for r in detailed["results"]))
        diff = self.run_cli("push", "--all", "--dry-run", "--diff")
        self.assertEqual(len(diff["results"]), 1)
        self.assertIn("+a very large body", diff["results"][0]["diff"])

    def test_failure_keeps_path_error_and_created_id_and_stops_batch(self):
        for name in ("a.md", "b.md", "c.md"):
            (self.root / name).write_text("body")
        create = self.notebook.store.create
        def create_or_fail(title, body, *, path):
            if path == "b.md":
                raise CreatedNoteError("recover-this-id", "verification failed")
            return create(title, body, path=path)
        with patch.object(self.notebook.store, "create", side_effect=create_or_fail):
            result = self.run_cli("push", "--all", expected=1)
        self.assertEqual(result["processed"], 2)
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["error"], 1)
        self.assertEqual(len(result["results"]), 1)
        failure = result["results"][0]
        self.assertEqual(failure["file"], "b.md")
        self.assertEqual(failure["id"], "recover-this-id")
        self.assertIn("verification failed", failure["error"])
        self.assertEqual(len(self.notebook.store.list_notes()), 1)

    def test_preserved_changes_and_missing_files_stay_visible(self):
        for name in ("local.md", "remote.md", "missing.md"):
            (self.root / name).write_text("base")
        self.copy.transfer("push", all_files=True)
        (self.root / "local.md").write_text("local edits")
        (self.root / "missing.md").unlink()
        state = json.loads(self.copy.state_path.read_text())
        note = self.notebook.store.read(state["files"]["remote.md"]["id"])
        self.notebook.store.update(note.id, "remote edits", revision=note.revision)
        result = self.run_cli("push", "--all")
        self.assertEqual([r["status"] for r in result["results"]], ["skipped_missing", "remote_changes"])
        (self.root / "local.md").write_text("more local edits")
        result = self.run_cli("pull", "--all")
        self.assertEqual([r["status"] for r in result["results"]], ["local_changes", "skipped_missing"])

    def test_single_note_retains_id_and_diff_is_opt_in(self):
        (self.root / "note.md").write_text("body")
        preview = self.run_cli("push", "note.md", "--dry-run")
        self.assertNotIn("diff", preview["results"][0])
        result = self.run_cli("push", "note.md")
        self.assertTrue(result["results"][0]["id"])
        self.assertNotIn("processed", result)

    def test_mirror_preview_lists_removals_without_diff_and_apply_is_compact(self):
        from dataclasses import replace
        self.notebook.config = replace(self.notebook.config, notes_dir="notes")
        root = self.root / "notes"
        root.mkdir()
        (root / "draft.md").write_text("local")
        self.notebook.store.create("Note", "remote", path="note.md")
        with patch("shynote.mirror._diff", side_effect=AssertionError("diff not requested")):
            preview = self.run_cli("pull", "--mirror", "--dry-run")
        self.assertEqual(preview["results"], [{"file": "draft.md", "status": "would_remove"},
                                              {"file": "note.md", "status": "would_pull"}])
        self.assertTrue((root / "draft.md").exists())
        diff = self.run_cli("pull", "--mirror", "--dry-run", "--diff")
        self.assertIn("-local", diff["results"][0]["diff"])
        result = self.run_cli("pull", "--mirror")
        self.assertEqual(result, {"operation": "pull", "mirror": True, "dry_run": False,
                                  "processed": 2, "removed": 1, "pulled": 1})

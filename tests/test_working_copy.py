from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from shynote.cli import main
from shynote.model import ShyNoteError
from shynote.notebook import open_notebook
from shynote.working_copy import WorkingCopy
from tests.fakes import FakeNotion, FakeS3


FIXTURES = Path(__file__).parent / "fixtures"


class WorkingCopyTests(unittest.TestCase):
    @contextmanager
    def notebook(self, backend):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".shynote").write_bytes((FIXTURES / f"{backend}-repo" / ".shynote").read_bytes())
            notebook = open_notebook(root, s3_client=FakeS3(), notion_transport=FakeNotion())
            yield root, notebook, WorkingCopy(notebook)

    def one(self, copy, operation, file="note.md", **kwargs):
        return copy.transfer(operation, file, **kwargs)["results"][0]

    def update_remote(self, notebook, note_id, body):
        note = notebook.store.read(note_id)
        guard = {"revision": note.revision} if notebook.store.capabilities.conditional_writes else {"unconditional": True}
        notebook.store.update(note_id, body, **guard)

    def test_round_trip_and_tracking_survive_reopening_on_both_backends(self):
        for backend in ("s3", "notion"):
            with self.subTest(backend=backend), self.notebook(backend) as (root, notebook, copy):
                path = root / "note.md"
                path.write_text("# café 日本語\n", encoding="utf-8")
                created = self.one(copy, "push", title="Finding")
                self.assertEqual(created["status"], "created")
                self.assertEqual(notebook.store.read(created["id"]).title, "Finding")
                path.write_text("local update\n", encoding="utf-8")
                pushed = self.one(WorkingCopy(notebook), "push", unconditional=True)
                self.assertEqual(pushed["status"], "pushed")
                self.assertEqual(notebook.store.read(created["id"]).body, "local update\n")
                self.update_remote(notebook, created["id"], "remote update\n")
                pulled = self.one(copy, "pull", show_diff=True)
                self.assertEqual(pulled["status"], "pulled")
                self.assertIn("-local update\n+remote update\n", pulled["diff"])
                self.assertEqual(path.read_text(), "remote update\n")
                self.assertEqual(self.one(copy, "pull")["status"], "unchanged")
                self.assertEqual(self.one(copy, "push")["status"], "unchanged")

    def test_dry_runs_do_not_write_remote_local_or_tracking_files(self):
        for backend in ("s3", "notion"):
            with self.subTest(backend=backend), self.notebook(backend) as (root, notebook, copy):
                path = root / "note.md"
                path.write_text("initial\n")
                preview = self.one(copy, "push", dry_run=True, show_diff=True)
                self.assertEqual(preview["status"], "would_create")
                self.assertIn("+initial\n", preview["diff"])
                self.assertFalse(copy.directory.exists())
                self.assertEqual(notebook.store.list_notes(), [])
                note_id = self.one(copy, "push")["id"]
                state = copy.state_path.read_bytes()
                path.write_text("changed\n")
                preview = self.one(copy, "push", dry_run=True, show_diff=True, unconditional=True)
                self.assertEqual(preview["status"], "would_push")
                self.assertIn("-initial\n+changed\n", preview["diff"])
                self.assertEqual(notebook.store.read(note_id).body, "initial\n")
                self.assertEqual(copy.state_path.read_bytes(), state)
                path.write_text("initial\n")
                self.update_remote(notebook, note_id, "remote\n")
                preview = self.one(copy, "pull", dry_run=True, show_diff=True)
                self.assertEqual(preview["status"], "would_pull")
                self.assertIn("-initial\n+remote\n", preview["diff"])
                self.assertEqual(path.read_text(), "initial\n")
                self.assertEqual(copy.state_path.read_bytes(), state)
                self.assertFalse((copy.directory / "lock").exists())

    def test_first_pull_fetches_selected_note_and_refuses_untracked_overwrite(self):
        for backend in ("s3", "notion"):
            with self.subTest(backend=backend), self.notebook(backend) as (root, notebook, copy):
                remote = notebook.store.create("Remote", "body\n", path="notes/new.md")
                preview = self.one(copy, "pull", "notes/new.md", note_id=remote.id, dry_run=True)
                self.assertEqual(preview["status"], "would_pull")
                self.assertFalse((root / "notes").exists())
                self.assertFalse(copy.directory.exists())
                (root / "notes").mkdir()
                (root / "notes/new.md").write_text("scratch\n")
                rejected = self.one(copy, "pull", "notes/new.md", note_id=remote.id)
                self.assertEqual(rejected["status"], "conflict")
                self.assertEqual((root / "notes/new.md").read_text(), "scratch\n")
                (root / "notes/new.md").unlink()
                pulled = self.one(copy, "pull", "notes/new.md", note_id=remote.id)
                self.assertEqual(pulled["status"], "pulled")
                self.assertEqual((root / "notes/new.md").read_text(), remote.body)
                duplicate = self.one(copy, "pull", "duplicate.md", note_id=remote.id)
                self.assertEqual(duplicate["status"], "error")
                self.assertFalse((root / "duplicate.md").exists())

    def test_conflicts_preserve_both_sides_and_state_even_when_unconditional(self):
        for backend in ("s3", "notion"):
            with self.subTest(backend=backend), self.notebook(backend) as (root, notebook, copy):
                path = root / "note.md"
                path.write_text("base\n")
                note_id = self.one(copy, "push")["id"]
                original_state = copy.state_path.read_bytes()
                path.write_text("local\n")
                self.update_remote(notebook, note_id, "remote\n")
                for operation in ("pull", "push"):
                    for dry_run in (False, True):
                        result = self.one(copy, operation, dry_run=dry_run, show_diff=True, unconditional=True)
                        self.assertEqual(result["status"], "conflict")
                        self.assertIn("local", result["diff"])
                        self.assertIn("remote", result["diff"])
                        self.assertEqual(path.read_text(), "local\n")
                        self.assertEqual(notebook.store.read(note_id).body, "remote\n")
                        self.assertEqual(copy.state_path.read_bytes(), original_state)
                # Explicitly reconciling the file to the remote content clears the conflict.
                path.write_text("remote\n")
                self.assertEqual(self.one(copy, "pull")["status"], "unchanged")

    def test_one_sided_changes_are_not_overwritten_in_wrong_direction(self):
        with self.notebook("s3") as (root, notebook, copy):
            path = root / "note.md"
            path.write_text("base")
            note_id = self.one(copy, "push")["id"]
            path.write_text("local")
            self.assertEqual(self.one(copy, "pull")["status"], "local_changes")
            self.assertEqual(path.read_text(), "local")
            path.write_text("base")
            self.update_remote(notebook, note_id, "remote")
            self.assertEqual(self.one(copy, "push")["status"], "remote_changes")
            self.assertEqual(notebook.store.read(note_id).body, "remote")

    def test_all_stops_at_first_conflict_and_keeps_earlier_progress(self):
        for backend in ("s3", "notion"):
            with self.subTest(backend=backend), self.notebook(backend) as (root, notebook, copy):
                ids = {}
                for name in ("a.md", "b.md", "c.md", "missing.md"):
                    (root / name).write_text("base")
                    ids[name] = self.one(copy, "push", name)["id"]
                (root / "scratch.md").write_text("untracked")
                unseen = notebook.store.create("Unseen remote", "unseen", path="note.md")
                (root / "missing.md").unlink()
                (root / "b.md").write_text("local conflict")
                self.update_remote(notebook, ids["b.md"], "remote conflict")
                (root / "a.md").write_text("safe update")
                (root / "c.md").write_text("later update")
                results = copy.transfer("push", all_files=True, unconditional=True)["results"]
                self.assertEqual([r["file"] for r in results], ["a.md", "b.md"])
                self.assertEqual([r["status"] for r in results], ["pushed", "conflict"])
                self.assertEqual(notebook.store.read(ids["a.md"]).body, "safe update")
                self.assertEqual(notebook.store.read(ids["c.md"]).body, "base")
                self.assertEqual(self.one(copy, "push", "a.md")["status"], "unchanged")
                self.assertFalse(notebook.store.read(ids["missing.md"]).archived)
                self.update_remote(notebook, ids["a.md"], "pull update")
                results = copy.transfer("pull", all_files=True)["results"]
                self.assertEqual([r["status"] for r in results], ["pulled", "conflict"])
                self.assertEqual((root / "a.md").read_text(), "pull update")
                self.assertEqual((root / "c.md").read_text(), "later update")
                self.assertEqual(self.one(copy, "pull", "a.md")["status"], "unchanged")
                (root / "b.md").write_text("remote conflict")
                results = copy.transfer("pull", all_files=True)["results"]
                self.assertEqual([r["status"] for r in results], ["unchanged", "unchanged", "local_changes", "skipped_missing"])
                self.assertEqual((root / "scratch.md").read_text(), "untracked")
                state = json.loads(copy.state_path.read_text())
                self.assertNotIn(unseen.id, [entry["id"] for entry in state["files"].values()])
                self.assertEqual(set(state["files"]), set(ids))

    def test_push_all_discovers_new_notes_and_tracks_them_on_both_backends(self):
        for backend in ("s3", "notion"):
            with self.subTest(backend=backend), self.notebook(backend) as (root, notebook, _):
                notebook.config = replace(notebook.config, notes_dir=".agent/notes")
                copy = WorkingCopy(notebook)
                (copy.root / "nested").mkdir(parents=True)
                (copy.root / "a.md").write_text("first")
                (copy.root / "nested/b.MD").write_text("second")
                (copy.root / "scratch.txt").write_text("not Markdown")
                (root / "outside.md").write_text("outside notes directory")
                self.assertEqual(copy.transfer("pull", all_files=True, dry_run=True)["results"], [])
                with patch("shynote.cli.open_notebook", return_value=notebook):
                    with redirect_stdout(StringIO()) as output:
                        self.assertEqual(main(["push", "--all", "--dry-run", "--diff"]), 0)
                    preview = json.loads(output.getvalue())["results"]
                    self.assertEqual([r["file"] for r in preview], ["a.md", "nested/b.MD"])
                    self.assertTrue(all(r["status"] == "would_create" and r["diff"] for r in preview))
                    self.assertFalse(copy.directory.exists())
                    self.assertEqual(notebook.store.list_notes(), [])
                    with redirect_stdout(StringIO()) as output:
                        self.assertEqual(main(["push", "--all", "--verbose"]), 0)
                    created = json.loads(output.getvalue())["results"]
                self.assertEqual([r["status"] for r in created], ["created", "created"])
                self.assertEqual({note.title for note in notebook.store.list_notes()}, {"a", "b"})
                repeated = copy.transfer("push", all_files=True)["results"]
                self.assertEqual([r["status"] for r in repeated], ["unchanged", "unchanged"])
                self.assertEqual([r["id"] for r in repeated], [r["id"] for r in created])
                self.assertEqual(set(json.loads(copy.state_path.read_text())["files"]), {"a.md", "nested/b.MD"})

    def test_push_all_mixes_new_notes_with_tracked_files_and_missing_paths(self):
        for backend in ("s3", "notion"):
            with self.subTest(backend=backend), self.notebook(backend) as (root, notebook, copy):
                (root / "existing.txt").write_text("base")
                note_id = self.one(copy, "push", "existing.txt")["id"]
                (root / "missing.md").write_text("retained remotely")
                missing_id = self.one(copy, "push", "missing.md")["id"]
                (root / "missing.md").unlink()
                (root / "existing.txt").write_text("changed")
                (root / "new.md").write_text("new")
                state = copy.state_path.read_bytes()
                preview = copy.transfer("push", all_files=True, dry_run=True, unconditional=True)["results"]
                self.assertEqual([r["status"] for r in preview], ["would_push", "skipped_missing", "would_create"])
                self.assertEqual(copy.state_path.read_bytes(), state)
                self.assertEqual(len(notebook.store.list_notes()), 2)
                results = copy.transfer("push", all_files=True, unconditional=True)["results"]
                self.assertEqual([r["file"] for r in results], ["existing.txt", "missing.md", "new.md"])
                self.assertEqual([r["status"] for r in results], ["pushed", "skipped_missing", "created"])
                self.assertEqual(notebook.store.read(note_id).body, "changed")
                self.assertFalse(notebook.store.read(missing_id).archived)
                (root / "untracked.md").write_text("local draft")
                pulled = copy.transfer("pull", all_files=True)["results"]
                self.assertEqual([r["file"] for r in pulled], [r["file"] for r in results])
                self.assertEqual((root / "untracked.md").read_text(), "local draft")

    def test_push_all_discovery_excludes_metadata_symlinks_and_non_files(self):
        with self.notebook("s3") as (root, notebook, copy):
            (root / "note.md").write_text("body")
            (root / ".gitignore").write_text("*.md\n")
            for name in (".git", ".shynote-local", "nested/.git", "nested/.shynote"):
                directory = root / name
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "hidden.md").write_text("metadata")
            (root / "linked.md").symlink_to(root / "note.md")
            (root / "linked-dir").symlink_to(root / ".git", target_is_directory=True)
            (root / "broken.md").symlink_to(root / "absent")
            (root / "empty.md").mkdir()
            results = copy.transfer("push", all_files=True)["results"]
            self.assertEqual([r["file"] for r in results], ["note.md"])
            self.assertEqual([note.title for note in notebook.store.list_notes()], ["note"])

    def test_push_all_new_files_stop_at_first_error_including_preview(self):
        for backend in ("s3", "notion"):
            with self.subTest(backend=backend), self.notebook(backend) as (root, notebook, copy):
                (root / "a.md").write_text("first")
                (root / "b.md").write_bytes(b"\xff")
                (root / "c.md").write_text("later")
                preview = copy.transfer("push", all_files=True, dry_run=True)["results"]
                self.assertEqual([r["status"] for r in preview], ["would_create", "error"])
                self.assertFalse(copy.directory.exists())
                self.assertEqual(notebook.store.list_notes(), [])
                results = copy.transfer("push", all_files=True)["results"]
                self.assertEqual([r["file"] for r in results], ["a.md", "b.md"])
                self.assertEqual([r["status"] for r in results], ["created", "error"])
                self.assertEqual([note.title for note in notebook.store.list_notes()], ["a"])
                self.assertEqual(set(json.loads(copy.state_path.read_text())["files"]), {"a.md"})

    def test_notion_updates_require_explicit_unconditional_flag(self):
        with self.notebook("notion") as (root, notebook, copy):
            path = root / "note.md"
            path.write_text("base")
            note_id = self.one(copy, "push")["id"]
            path.write_text("edit")
            for dry_run in (True, False):
                result = self.one(copy, "push", dry_run=dry_run)
                self.assertEqual(result["status"], "error")
                self.assertIn("--unconditional", result["error"])
                self.assertEqual(notebook.store.read(note_id).body, "base")
            self.assertEqual(self.one(copy, "push", unconditional=True)["status"], "pushed")

    def test_s3_race_after_preview_is_rechecked_on_real_push(self):
        with self.notebook("s3") as (root, notebook, copy):
            path = root / "note.md"
            path.write_text("base")
            note_id = self.one(copy, "push")["id"]
            path.write_text("local")
            self.assertEqual(self.one(copy, "push", dry_run=True)["status"], "would_push")
            self.update_remote(notebook, note_id, "other writer")
            self.assertEqual(self.one(copy, "push")["status"], "conflict")
            self.assertEqual(notebook.store.read(note_id).body, "other writer")

    def test_s3_atomic_conflict_does_not_advance_tracking(self):
        with self.notebook("s3") as (root, notebook, copy):
            path = root / "note.md"
            path.write_text("base")
            note_id = self.one(copy, "push")["id"]
            state = copy.state_path.read_bytes()
            path.write_text("local")
            notebook.store.client.before_put = lambda: self.update_remote(notebook, note_id, "race")
            self.assertEqual(self.one(copy, "push")["status"], "conflict")
            self.assertEqual(copy.state_path.read_bytes(), state)
            self.assertEqual(notebook.store.read(note_id).body, "race")

    def test_pull_detects_local_edit_during_remote_read(self):
        with self.notebook("s3") as (root, notebook, copy):
            path = root / "note.md"
            path.write_text("base")
            note_id = self.one(copy, "push")["id"]
            state = copy.state_path.read_bytes()
            self.update_remote(notebook, note_id, "remote")
            read = notebook.store.read

            def concurrent_edit(note_id):
                path.write_text("editor change")
                return read(note_id)

            with patch.object(notebook.store, "read", side_effect=concurrent_edit):
                self.assertEqual(self.one(copy, "pull")["status"], "conflict")
            self.assertEqual(path.read_text(), "editor change")
            self.assertEqual(copy.state_path.read_bytes(), state)

    def test_readback_mismatch_does_not_claim_remote_is_synchronized(self):
        with self.notebook("notion") as (root, notebook, copy):
            path = root / "note.md"
            path.write_text("base")
            note_id = self.one(copy, "push")["id"]
            state = copy.state_path.read_bytes()
            path.write_text("local")
            update = notebook.store.update

            def changed_write(note_id, body, **kwargs):
                update(note_id, "normalized or concurrent content", **kwargs)
                return body  # The acknowledged write differs from a later writer's content.

            with patch.object(notebook.store, "update", side_effect=changed_write):
                result = self.one(copy, "push", unconditional=True)
            self.assertEqual(result["status"], "conflict")
            self.assertIn("write completed", result["error"])
            self.assertEqual(copy.state_path.read_bytes(), state)
            self.assertEqual(path.read_text(), "local")

    def test_notion_normalization_uses_acknowledged_content_and_retains_local_formatting(self):
        with self.notebook("notion") as (root, notebook, copy):
            notebook.store.transport.normalize_markdown = lambda body: body.replace("\n\n", "\n").rstrip("\n")
            path = root / "note.md"
            path.write_text("# Heading\n\nInitial body.\n")
            note_id = self.one(copy, "push")["id"]
            local = "# Heading\n\nUpdated body.\n"
            path.write_text(local)
            self.assertEqual(self.one(copy, "push", unconditional=True)["status"], "pushed")
            self.assertEqual(path.read_text(), local)
            self.assertEqual(notebook.store.read(note_id).body, "# Heading\nUpdated body.")
            self.assertEqual(self.one(copy, "push", unconditional=True)["status"], "unchanged")
            self.assertEqual(self.one(copy, "pull")["status"], "unchanged")
            self.update_remote(notebook, note_id, "# Heading\nRemote body.")
            self.assertEqual(self.one(copy, "pull")["status"], "pulled")
            self.assertEqual(path.read_text(), "# Heading\nRemote body.")

    def test_incomplete_notion_write_acknowledgment_does_not_advance_tracking(self):
        with self.notebook("notion") as (root, notebook, copy):
            path = root / "note.md"
            path.write_text("base")
            self.one(copy, "push")
            state = copy.state_path.read_bytes()
            path.write_text("edit")
            request = notebook.store.transport.request

            def incomplete_write(method, route, payload=None):
                result = request(method, route, payload)
                if method == "PATCH":
                    result["truncated"] = True
                return result

            with patch.object(notebook.store.transport, "request", side_effect=incomplete_write):
                result = self.one(copy, "push", unconditional=True)
            self.assertEqual(result["status"], "error")
            self.assertIn("acknowledge complete Markdown", result["error"])
            self.assertEqual(copy.state_path.read_bytes(), state)

    def test_state_identity_corruption_and_lock_fail_without_remote_changes(self):
        with self.notebook("s3") as (root, notebook, copy):
            (root / "note.md").write_text("base")
            self.one(copy, "push")
            notebook.config = replace(notebook.config, notebook="different")
            with self.assertRaisesRegex(ShyNoteError, "different notebook"):
                WorkingCopy(notebook).transfer("push", "note.md")
            copy.state_path.write_text("broken json")
            with self.assertRaisesRegex(ShyNoteError, "Invalid local tracking"):
                copy.transfer("pull", all_files=True)
            self.assertFalse((copy.directory / "lock").exists())
            (copy.directory / "lock").mkdir()
            with self.assertRaisesRegex(ShyNoteError, "Another transfer"):
                copy.transfer("push", "note.md")

    def test_metadata_outside_paths_and_symlinks_are_rejected(self):
        with self.notebook("s3") as (root, notebook, copy):
            (root / "note.md").write_text("base")
            (root / "linked.md").symlink_to(root / "note.md")
            for name in ("../outside.md", ".shynote", ".shynote-local/state.json", ".git/config", "linked.md"):
                with self.subTest(name=name):
                    self.assertEqual(self.one(copy, "push", name)["status"], "error")
            self.assertEqual(notebook.store.list_notes(), [])

    def test_diff_preserves_missing_newline_and_pull_preserves_file_mode(self):
        with self.notebook("s3") as (root, notebook, copy):
            path = root / "note.md"
            path.write_bytes(b"base\r\n")
            path.chmod(0o640)
            note_id = self.one(copy, "push")["id"]
            self.update_remote(notebook, note_id, "no final newline")
            result = self.one(copy, "pull", show_diff=True)
            self.assertIn("\\ No newline at end of file", result["diff"])
            self.assertEqual(path.read_bytes(), b"no final newline")
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_cli_flags_json_diffs_and_conflict_exit_status(self):
        with self.notebook("s3") as (root, notebook, copy):
            (root / "note.md").write_text("base\n")
            with patch("shynote.cli.open_notebook", return_value=notebook):
                with redirect_stdout(StringIO()) as output:
                    self.assertEqual(main(["push", "note.md", "--dry-run", "--diff"]), 0)
                self.assertIn("+base\n", json.loads(output.getvalue())["results"][0]["diff"])
                note_id = self.one(copy, "push")["id"]
                (root / "note.md").write_text("local\n")
                self.update_remote(notebook, note_id, "remote\n")
                with redirect_stdout(StringIO()) as output:
                    self.assertEqual(main(["pull", "--all", "--diff"]), 1)
                self.assertEqual(json.loads(output.getvalue())["results"][0]["status"], "conflict")

    def test_cli_multiple_files_dry_run_creation_order_and_deduplication(self):
        for backend in ("s3", "notion"):
            with self.subTest(backend=backend), self.notebook(backend) as (root, notebook, copy):
                for name in ("hello1.md", "hello2.md", "scratch.md"):
                    (root / name).write_text(name + "\n")
                with patch("shynote.cli.open_notebook", return_value=notebook):
                    with redirect_stdout(StringIO()) as output:
                        self.assertEqual(main(["push", "hello2.md", "hello1.md", "./hello2.md", "--dry-run", "--diff"]), 0)
                    results = json.loads(output.getvalue())["results"]
                    self.assertEqual([r["file"] for r in results], ["hello2.md", "hello1.md"])
                    self.assertTrue(all(r["status"] == "would_create" and r["diff"] for r in results))
                    self.assertFalse(copy.directory.exists())
                    self.assertEqual(notebook.store.list_notes(), [])
                    with redirect_stdout(StringIO()) as output:
                        self.assertEqual(main(["push", "hello2.md", "hello1.md", "hello2.md", "--verbose"]), 0)
                    results = json.loads(output.getvalue())["results"]
                    self.assertEqual([r["status"] for r in results], ["created", "created"])
                    self.assertEqual({n.title for n in notebook.store.list_notes()}, {"hello1", "hello2"})
                    self.assertEqual(set(json.loads(copy.state_path.read_text())["files"]), {"hello1.md", "hello2.md"})

    def test_multiple_selected_files_stop_at_first_conflict(self):
        for backend in ("s3", "notion"):
            with self.subTest(backend=backend), self.notebook(backend) as (root, notebook, copy):
                ids = {}
                for name in ("conflict.md", "safe.md", "unselected.md"):
                    (root / name).write_text("base\n")
                    ids[name] = self.one(copy, "push", name)["id"]
                    self.update_remote(notebook, ids[name], "remote\n")
                (root / "conflict.md").write_text("local\n")
                selection = ["conflict.md", "missing.md", "safe.md"]
                state = copy.state_path.read_bytes()
                with patch("shynote.cli.open_notebook", return_value=notebook):
                    with redirect_stdout(StringIO()) as output:
                        self.assertEqual(main(["pull", *selection, "--dry-run", "--diff"]), 1)
                    results = json.loads(output.getvalue())["results"]
                    self.assertEqual([r["status"] for r in results], ["conflict"])
                    self.assertEqual(results[0]["file"], "conflict.md")
                    self.assertIn("+remote", results[0]["diff"])
                    self.assertEqual(copy.state_path.read_bytes(), state)
                    self.assertEqual((root / "safe.md").read_text(), "base\n")
                    with redirect_stdout(StringIO()) as output:
                        self.assertEqual(main(["pull", *selection, "--diff"]), 1)
                    self.assertEqual([r["status"] for r in json.loads(output.getvalue())["results"]], ["conflict"])
                    self.assertEqual((root / "conflict.md").read_text(), "local\n")
                    self.assertEqual((root / "safe.md").read_text(), "base\n")
                    self.assertEqual((root / "unselected.md").read_text(), "base\n")
                    (root / "safe.md").write_text("safe edit\n")
                    with redirect_stdout(StringIO()) as output:
                        self.assertEqual(main(["push", *selection, "--unconditional"]), 1)
                    self.assertEqual([r["status"] for r in json.loads(output.getvalue())["results"]], ["conflict"])
                    self.assertEqual(notebook.store.read(ids["safe.md"]).body, "remote\n")
                    self.assertEqual(notebook.store.read(ids["conflict.md"]).body, "remote\n")
                    self.assertEqual(notebook.store.read(ids["unselected.md"]).body, "remote\n")

    def test_explicit_push_stops_at_missing_file_including_dry_run(self):
        for backend in ("s3", "notion"):
            with self.subTest(backend=backend), self.notebook(backend) as (root, notebook, copy):
                for name in ("first.md", "later.md"):
                    (root / name).write_text("body")
                selection = ["first.md", "missing.md", "later.md"]
                preview = copy.transfer("push", selection, dry_run=True)["results"]
                self.assertEqual([r["status"] for r in preview], ["would_create", "error"])
                self.assertEqual(preview[-1]["file"], "missing.md")
                self.assertFalse(copy.directory.exists())
                results = copy.transfer("push", selection)["results"]
                self.assertEqual([r["status"] for r in results], ["created", "error"])
                self.assertEqual(results[-1]["file"], "missing.md")
                self.assertEqual([n.title for n in notebook.store.list_notes()], ["first"])
                self.assertEqual(set(json.loads(copy.state_path.read_text())["files"]), {"first.md"})

    def test_cli_rejects_invalid_commands_and_file_options_before_opening_notebook(self):
        for arguments in (
            ["create", "--title", "Title", "--file", "a.md"],
            ["push"], ["pull"], ["push", "a.md", "--all"], ["pull", "a.md", "b.md", "--all"],
            ["push", "a.md", "b.md", "--title", "Title"],
            ["pull", "a.md", "b.md", "--id", "note-id"],
            ["push", "--all", "--title", "Title"], ["pull", "--all", "--id", "note-id"],
        ):
            with self.subTest(arguments=arguments), patch("shynote.cli.open_notebook") as open_mock:
                with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
                    main(arguments)
                self.assertEqual(error.exception.code, 2)
                open_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()

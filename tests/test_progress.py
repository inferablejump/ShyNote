from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tqdm import tqdm

from shynote.cli import main
from shynote.model import ProviderError
from shynote.notebook import open_notebook
from tests.fakes import FakeNotion, FakeS3


class Terminal(StringIO):
    def isatty(self):
        return True


class ProgressTests(unittest.TestCase):
    @contextmanager
    def notebook(self, backend="s3"):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = Path(__file__).parent / "fixtures" / f"{backend}-repo" / ".shynote"
            (root / ".shynote").write_bytes(fixture.read_bytes())
            notebook = open_notebook(root, s3_client=FakeS3(), notion_transport=FakeNotion())
            with patch("shynote.cli.open_notebook", return_value=notebook):
                yield root, notebook

    def run_cli(self, arguments, err=None, expected=0):
        err = err if err is not None else StringIO()
        with redirect_stdout(StringIO()) as out, redirect_stderr(err):
            self.assertEqual(main(arguments), expected)
        return json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()

    def test_current_filename_visible_before_upload_on_both_backends(self):
        for backend in ("s3", "notion"):
            with self.subTest(backend=backend), self.notebook(backend) as (root, notebook):
                for name in ("a.md", "b.md"):
                    (root / name).write_text("body")
                err = Terminal()
                original = notebook.store.create
                calls = []
                def create(title, body, *, path):
                    self.assertIn(f"Pushing {len(calls)}/2", err.getvalue())
                    self.assertIn(path, err.getvalue().split("\r")[-1])
                    calls.append(path)
                    return original(title, body, path=path)
                with patch.object(notebook.store, "create", side_effect=create):
                    result, output = self.run_cli(["push", "--all"], err)
                self.assertEqual(result["created"], 2)
                self.assertEqual(calls, ["a.md", "b.md"])
                self.assertIn("Pushing 2/2", output)

    def test_nonterminal_default_quiet_and_both_overrides(self):
        with self.notebook() as (root, _):
            (root / "note.md").write_text("body")
            for stream, flags, visible in ((StringIO(), [], False), (Terminal(), ["--no-progress"], False),
                                           (StringIO(), ["--progress"], True)):
                with self.subTest(flags=flags):
                    result, output = self.run_cli(["push", "--all", "--dry-run", *flags], stream)
                    self.assertEqual(result["would_create"], 1)
                    self.assertEqual(bool(output), visible)
                    if visible:
                        self.assertIn("Previewing push 1/1", output)
                    self.assertFalse((root / ".shynote-local").exists())

    def test_failure_stops_at_selected_file_and_notices_remain_readable(self):
        with self.notebook() as (root, notebook):
            for name in ("a.md", "b.md", "c.md"):
                (root / name).write_text("body")
            original = notebook.store.create
            def create(title, body, *, path):
                if path == "b.md":
                    print("shynote: retry notice", file=sys.stderr, flush=True)
                    raise ProviderError("provider failed")
                return original(title, body, path=path)
            with patch.object(notebook.store, "create", side_effect=create):
                result, output = self.run_cli(["push", "--all"], Terminal(), expected=1)
            self.assertEqual(result["processed"], 2)
            self.assertEqual(result["results"][0]["file"], "b.md")
            self.assertIn("Pushing 2/3", output)
            self.assertNotIn("c.md", output)
            self.assertEqual(output.count("shynote: retry notice"), 1)

    def test_pull_and_mirror_display_note_before_read(self):
        for mirror in (False, True):
            with self.subTest(mirror=mirror), self.notebook("notion") as (root, notebook):
                notebook.config = replace(notebook.config, notes_dir="notes")
                note = notebook.store.create("Note", "remote", path="folder/note.md")
                err = Terminal()
                original = notebook.store.read
                def read(note_id):
                    self.assertIn("folder/note.md", err.getvalue().split("\r")[-1])
                    return original(note_id)
                selection = ["--mirror"] if mirror else [note.path, "--id", note.id]
                with patch.object(notebook.store, "read", side_effect=read):
                    result, output = self.run_cli(["pull", *selection], err)
                self.assertIn(("Fetching" if mirror else "Pulling") + " 1/1", output)
                self.assertEqual((root / "notes/folder/note.md").read_text(), "remote")

    def test_interrupt_closes_bar_and_restores_stderr(self):
        with self.notebook() as (root, notebook):
            (root / "note.md").write_text("body")
            original_instances = set(tqdm._instances)
            original_stderr = sys.stderr
            with patch.object(notebook.store, "create", side_effect=KeyboardInterrupt):
                result, output = self.run_cli(["push", "--all"], Terminal(), expected=130)
            self.assertIsNone(result)
            self.assertIn("shynote: cancelled.", output)
            self.assertEqual(set(tqdm._instances), original_instances)
            self.assertIs(sys.stderr, original_stderr)
            self.assertFalse((root / ".shynote-local/lock").exists())

    def test_empty_selection_and_deduplicated_total(self):
        with self.notebook() as (root, _):
            result, output = self.run_cli(["push", "--all"], Terminal())
            self.assertEqual(result["processed"], 0)
            self.assertIn("Pushing 0/0", output)
            (root / "note.md").write_text("body")
            result, output = self.run_cli(["push", "note.md", "./note.md"], Terminal())
            self.assertEqual(result["processed"], 1)
            self.assertIn("Pushing 1/1", output)

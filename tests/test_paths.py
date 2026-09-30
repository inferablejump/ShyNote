from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from shynote.cli import main
from shynote.model import ShyNoteError
from shynote.notebook import notebook_from_config, open_notebook
from shynote.stores import notion_metadata
from shynote.working_copy import WorkingCopy
from tests.fakes import FakeNotion, FakeS3

FIXTURES = Path(__file__).parent / "fixtures"


class RemotePathTests(unittest.TestCase):
    @contextmanager
    def checkouts(self, backend):
        with TemporaryDirectory() as directory:
            s3, notion = FakeS3(), FakeNotion()
            config = open_notebook(FIXTURES / f"{backend}-repo").config
            notebooks = []
            for name, notes_dir in (("original", ".agent/notes"), ("fresh", "scratch")):
                root = Path(directory) / name
                root.mkdir()
                notebooks.append(notebook_from_config(
                    replace(config, root=root, notes_dir=notes_dir),
                    s3_client=s3, notion_transport=notion))
            yield notebooks

    def cli(self, notebook, *args, expected=0):
        with patch("shynote.cli.open_notebook", return_value=notebook):
            with redirect_stdout(StringIO()) as out, redirect_stderr(StringIO()) as err:
                self.assertEqual(main(list(args)), expected, err.getvalue())
        return json.loads(out.getvalue()) if out.getvalue() else err.getvalue()

    def test_restore_hierarchy_from_ids_in_fresh_checkout_on_both_backends(self):
        for backend in ("s3", "notion"):
            with self.subTest(backend=backend), self.checkouts(backend) as (original, fresh):
                source, target = WorkingCopy(original), WorkingCopy(fresh)
                paths = {"design/日本語/auth.md": "design body\n", "research/auth.md": "research body\n"}
                for name, body in paths.items():
                    file = source.root / name
                    file.parent.mkdir(parents=True)
                    file.write_text(body)
                created = self.cli(original, "push", "--all")["results"]
                listing = self.cli(fresh, "list")
                self.assertEqual({n["path"] for n in listing}, set(paths))
                self.assertEqual({n["title"] for n in listing}, {"auth"})
                self.assertFalse(target.directory.exists())
                self.assertEqual(self.cli(fresh, "pull", "--all")["results"], [])
                for note in created:
                    preview = self.cli(fresh, "pull", "--id", note["id"], "--dry-run")["results"][0]
                    self.assertEqual(preview["file"], note["file"])
                    self.assertEqual(preview["status"], "would_pull")
                    self.assertFalse((target.root / note["file"]).exists())
                    result = self.cli(fresh, "pull", "--id", note["id"])["results"][0]
                    self.assertEqual(result["status"], "pulled")
                    self.assertEqual((target.root / note["file"]).read_text(), paths[note["file"]])
                    self.assertEqual(self.cli(fresh, "read", note["id"])["path"], note["file"])
                state = json.loads(target.state_path.read_text())
                self.assertEqual(set(state["files"]), set(paths))
                self.assertEqual({n["path"] for n in self.cli(fresh, "search-title", "auth")}, set(paths))
                file = target.root / created[0]["file"]
                file.write_text("updated body")
                result = self.cli(fresh, "push", created[0]["file"], "--unconditional")["results"][0]
                self.assertEqual(result["status"], "pushed")
                note = original.store.read(created[0]["id"])
                self.assertEqual(note.path, created[0]["file"])
                self.assertEqual(note.body, "updated body")

    def test_saved_path_does_not_overwrite_local_drafts_or_allow_aliases(self):
        for backend in ("s3", "notion"):
            with self.subTest(backend=backend), self.checkouts(backend) as (original, fresh):
                note = original.store.create("Title", "remote", path="nested/note.md")
                target = WorkingCopy(fresh)
                path = target.root / note.path
                path.parent.mkdir(parents=True)
                path.write_text("local draft")
                result = self.cli(fresh, "pull", "--id", note.id, expected=1)["results"][0]
                self.assertEqual(result["status"], "conflict")
                self.assertEqual(path.read_text(), "local draft")
                self.assertFalse(target.state_path.exists())
                result = self.cli(fresh, "pull", "alias.md", "--id", note.id, expected=1)["results"][0]
                self.assertIn("Remote note path", result["error"])
                self.assertFalse((target.root / "alias.md").exists())
                path.write_text("remote")
                self.assertEqual(self.cli(fresh, "pull", "--id", note.id)["results"][0]["status"], "pulled")

    def test_restoration_refuses_symlinks(self):
        for backend in ("s3", "notion"):
            with self.subTest(backend=backend), self.checkouts(backend) as (original, fresh):
                note = original.store.create("Title", "remote", path="nested/note.md")
                target = WorkingCopy(fresh)
                target.root.mkdir()
                outside = fresh.config.root / "outside"
                outside.mkdir()
                (target.root / "nested").symlink_to(outside, target_is_directory=True)
                result = self.cli(fresh, "pull", "--id", note.id, expected=1)["results"][0]
                self.assertIn("symbolic", result["error"])
                self.assertFalse((outside / "note.md").exists())

    def test_invalid_paths_cannot_be_published(self):
        invalid = (None, "", "/tmp/note.md", "../note.md", "a/../b.md", "a//b.md",
                   "a/./b.md", ".git/config", "a/.shynote-local/state.json", "C:/note.md",
                   "a\\b.md", "a\x00.md", "a\n.md", "x" * 1025)
        for backend in ("s3", "notion"):
            with self.checkouts(backend) as (original, _):
                for path in invalid:
                    with self.subTest(backend=backend, path=path), self.assertRaises(ShyNoteError):
                        original.store.create("Title", "body", path=path)
                self.assertEqual(original.store.list_notes(), [])

    def test_missing_or_unsafe_remote_metadata_fails_without_local_writes(self):
        for backend in ("s3", "notion"):
            for bad_path in (None, "../escape.md", "/tmp/escape.md", ".git/config"):
                with self.subTest(backend=backend, path=bad_path), self.checkouts(backend) as (original, fresh):
                    store = original.store
                    note = store.create("Title", "body", path="note.md")
                    if backend == "s3":
                        item = store.client.objects[(store.settings.bucket, store._key(note.id))]
                        _, encoded, body = item["Body"].decode().split("\n", 2)
                        header = json.loads(encoded)
                        if bad_path is None:
                            del header["path"]
                            del item["Metadata"]["shynote-path"]
                        else:
                            header["path"] = bad_path
                            item["Metadata"]["shynote-path"] = base64.b64encode(bad_path.encode()).decode()
                        item["Body"] = f"---\n{json.dumps(header)}\n{body}".encode()
                    else:
                        metadata = json.dumps({"version": 1, "kind": "note", "path": bad_path})
                        store.transport.bodies[note.id] = ("body" if bad_path is None else
                            f"```text\nshynote-metadata\n{metadata}\n```\n\nbody")
                    for command in (("read", note.id), ("list",), ("search-title", "Title"),
                                    ("pull", "--id", note.id, "--dry-run")):
                        error = self.cli(fresh, *command, expected=1)
                        self.assertIn("path", error.lower())
                    self.assertFalse(WorkingCopy(fresh).root.exists())
                    self.assertFalse(WorkingCopy(fresh).directory.exists())
                    with self.assertRaises(ShyNoteError):
                        store.update(note.id, "replacement", unconditional=True)

    def test_metadata_is_not_returned_in_body_and_survives_notion_normalization(self):
        body = '# Title\n\n```python\nprint("hi")\n```\n'
        encoded = notion_metadata.encode(body, "nested/café.md")
        decoded, path = notion_metadata.decode(encoded)
        self.assertEqual((decoded, path), (body, "nested/café.md"))
        normalized = encoded.replace("```text", "```plain text", 1).replace("\n\n", "\n").rstrip("\n")
        decoded, path = notion_metadata.decode(normalized)
        self.assertEqual(path, "nested/café.md")
        self.assertEqual(decoded, body.replace("\n\n", "\n").rstrip("\n"))
        for metadata in ('{}', '{"version": 2, "path": "note.md"}', 'broken',
                         '{"version": true, "path": "note.md"}'):
            with self.subTest(metadata=metadata), self.assertRaises(ShyNoteError):
                notion_metadata.decode(f"```text\nshynote-metadata\n{metadata}\n```\n\nbody")


if __name__ == "__main__":
    unittest.main()

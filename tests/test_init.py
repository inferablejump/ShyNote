from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import boto3
from botocore.stub import Stubber

from shynote.cli import main
from shynote.config import load_config
from shynote.initialize import _parent_id
from shynote.model import ConfigurationError, ProviderError, ShyNoteError
from shynote.notebook import notebook_from_config, open_notebook
from shynote.stores.s3 import S3Store
from shynote.working_copy import WorkingCopy
from tests.fakes import FakeNotion, FakeS3


PARENT = "00000000-0000-4000-8000-000000000001"
FIXTURES = Path(__file__).parent / "fixtures"


class InitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.s3 = FakeS3()
        self.notion = FakeNotion()
        self.notion.pages[PARENT] = {"id": PARENT, "in_trash": False,
                                     "parent": {"page_id": "outside"}}
        self.opened = []

    def factory(self, config):
        notebook = notebook_from_config(config, s3_client=self.s3, notion_transport=self.notion)
        self.opened.append(notebook)
        return notebook

    def flags(self, backend):
        return (["--backend", "aws", "--bucket", "test-bucket", "--region", "us-east-1"]
                if backend == "s3" else ["--backend", "notion", "--parent-page", PARENT])

    def run_cli(self, arguments, *, tty=False, answers=None):
        with patch("shynote.initialize.notebook_from_config", side_effect=self.factory), \
                patch("sys.stdin.isatty", return_value=tty), \
                patch("builtins.input", side_effect=answers if answers is not None else AssertionError("Unexpected prompt")), \
                redirect_stdout(StringIO()) as out, redirect_stderr(StringIO()) as err:
            code = main(arguments)
        return code, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()

    def test_complete_flags_check_empty_storage_without_prompts_or_remote_writes(self):
        for backend in ("s3", "notion"):
            with self.subTest(backend=backend):
                root = self.root / backend
                root.mkdir()
                code, result, error = self.run_cli(["init", "--repo", str(root), *self.flags(backend)], tty=True)
                self.assertEqual(code, 0, error)
                self.assertEqual(result["status"], "initialized")
                self.assertEqual(result["read_access"], "verified")
                self.assertEqual(result["write_access"], "not_tested")
                config = load_config(root)
                self.assertEqual(config.backend, backend)
                self.assertEqual(config.notes_dir, ".agent/notes")
                self.assertTrue((root / ".agent/notes").is_dir())
                self.assertFalse((root / ".shynote-local").exists())
                self.assertEqual((root / ".gitignore").read_text(), "/.shynote-local/\n/.agent/notes/\n")
        self.assertEqual(len(self.s3.calls), 1)
        self.assertEqual(self.s3.calls[0][0], "LIST")
        self.assertEqual(len(self.notion.calls), 2)
        self.assertTrue(all(call[0] == "GET" for call in self.notion.calls))

    def test_s3_access_probe_matches_sdk_and_handles_empty_prefix(self):
        config = load_config(FIXTURES / "s3-repo")
        client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")
        with Stubber(client) as stub:
            stub.add_response("list_objects_v2", {"IsTruncated": False, "KeyCount": 0},
                              {"Bucket": config.storage.bucket, "Prefix": "fixtures/fixture-s3/notes/", "MaxKeys": 1})
            S3Store(config, client=client).check_access()
            stub.assert_no_pending_responses()

    def test_wizard_prefills_flags_shows_preview_and_applies(self):
        code, result, error = self.run_cli(
            ["--repo", str(self.root), "init", "--backend", "notion"],
            tty=True, answers=["", f"https://app.notion.com/p/test/Page-{PARENT.replace('-', '')}?source=copy_link", "", "yes"])
        self.assertEqual(code, 0, error)
        self.assertEqual(result["status"], "initialized")
        self.assertIn("Read access verified", error)
        self.assertIn("Apply this setup?", error)
        self.assertIn('notes_dir = ".agent/notes"', error)
        self.assertNotIn("Backend (", error)
        self.assertEqual(load_config(self.root).storage.parent_page_id, PARENT)

    def test_wizard_s3_collects_missing_settings_and_ignore_opt_out(self):
        code, result, error = self.run_cli(["init", "--repo", str(self.root)], tty=True,
                                         answers=["notes", "aws", "test-bucket", "us-west-2", "", "no", "yes"])
        self.assertEqual(code, 0, error)
        config = load_config(self.root)
        self.assertEqual(config.storage.region, "us-west-2")
        self.assertEqual(config.storage.prefix, "shynote")
        self.assertEqual(config.notes_dir, "notes")
        self.assertEqual(result["gitignore_added"], ["/.shynote-local/"])

    def test_cancelled_wizard_leaves_setup_untouched(self):
        (self.root / ".gitignore").write_bytes(b"existing\r\n")
        before = list(self.root.iterdir())
        code, result, error = self.run_cli(["init", "--repo", str(self.root), "--backend", "notion"],
                                         tty=True, answers=["", PARENT, "", "no"])
        self.assertEqual(code, 0, error)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(list(self.root.iterdir()), before)
        self.assertEqual((self.root / ".gitignore").read_bytes(), b"existing\r\n")

    def test_closed_input_and_interrupt_do_not_create_config(self):
        for exception, status in ((EOFError(), 1), (KeyboardInterrupt(), 130)):
            with self.subTest(exception=type(exception)):
                code, _, _ = self.run_cli(["init", "--repo", str(self.root)], tty=True, answers=exception)
                self.assertEqual(code, status)
                self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_arguments_never_prompt_without_terminal_or_when_disabled(self):
        for tty, flags in ((False, []), (True, ["--non-interactive"])):
            with self.subTest(tty=tty):
                code, _, error = self.run_cli(["init", "--repo", str(self.root), "--backend", "aws", *flags], tty=tty)
                self.assertEqual(code, 1)
                self.assertIn("--bucket", error)
                self.assertIn("--region", error)
                self.assertEqual(self.opened, [])
                self.assertEqual(list(self.root.iterdir()), [])

    def test_credential_or_access_failure_leaves_existing_files_untouched(self):
        (self.root / ".gitignore").write_text("keep\n")
        notes = self.root / "notes"
        notes.mkdir()
        (notes / "draft.md").write_text("scratch")
        with patch("shynote.stores.s3.S3Store.check_access", side_effect=ProviderError("Credential failed")):
            code, _, error = self.run_cli(["init", "--repo", str(self.root), "--notes-dir", "notes", *self.flags("s3")])
        self.assertEqual(code, 1)
        self.assertIn("Credential failed", error)
        self.assertFalse((self.root / ".shynote").exists())
        self.assertEqual((self.root / ".gitignore").read_text(), "keep\n")
        self.assertEqual((notes / "draft.md").read_text(), "scratch")
        self.assertFalse((self.root / ".shynote-local").exists())

    def test_archived_notion_parent_is_rejected(self):
        self.notion.pages[PARENT]["in_trash"] = True
        code, _, error = self.run_cli(["init", "--repo", str(self.root), *self.flags("notion")])
        self.assertEqual(code, 1)
        self.assertIn("archived or in trash", error)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_existing_marker_is_not_overwritten_or_reauthenticated(self):
        original = (FIXTURES / "s3-repo/.shynote").read_bytes()
        (self.root / ".shynote").write_bytes(original)
        nested = self.root / "nested"
        nested.mkdir()
        code, result, error = self.run_cli(["init", "--repo", str(nested), *self.flags("notion")])
        self.assertEqual(code, 0, error)
        self.assertEqual(result["status"], "already_initialized")
        self.assertEqual(result["backend"], "s3")
        self.assertEqual(self.opened, [])
        self.assertEqual((self.root / ".shynote").read_bytes(), original)
        self.assertFalse((nested / ".shynote").exists())

    def test_git_root_discovery_and_explicit_directory_override(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        nested = self.root / "nested"
        nested.mkdir()
        with patch("shynote.initialize.Path.cwd", return_value=nested):
            code, result, error = self.run_cli(["init", *self.flags("s3")])
        self.assertEqual(code, 0, error)
        self.assertEqual(result["root"], str(self.root))
        (self.root / ".shynote").unlink()
        code, result, error = self.run_cli(["init", "--repo", str(nested), *self.flags("s3")])
        self.assertEqual(code, 0, error)
        self.assertEqual(result["root"], str(nested))

    def test_non_git_root_falls_back_to_current_directory(self):
        with patch("shynote.initialize.Path.cwd", return_value=self.root):
            code, result, error = self.run_cli(["init", *self.flags("s3")])
        self.assertEqual(code, 0, error)
        self.assertEqual(result["root"], str(self.root))

    def test_ignore_existing_content_tracked_warning_and_no_automatic_push(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        notes = self.root / ".agent/notes"
        notes.mkdir(parents=True)
        (notes / "existing.md").write_text("keep me")
        subprocess.run(["git", "-C", str(self.root), "add", ".agent/notes/existing.md"], check=True)
        (self.root / ".gitignore").write_bytes(b"existing\r\n/.shynote-local/\r\n")
        code, result, error = self.run_cli(["init", "--repo", str(self.root), *self.flags("s3")])
        self.assertEqual(code, 0, error)
        self.assertTrue(result["warnings"])
        self.assertEqual(result["gitignore_added"], ["/.agent/notes/"])
        self.assertTrue((self.root / ".gitignore").read_bytes().startswith(b"existing\r\n/.shynote-local/\r\n"))
        self.assertEqual((notes / "existing.md").read_text(), "keep me")
        self.assertEqual(self.s3.objects, {})
        self.assertFalse((self.root / ".shynote-local").exists())
        tracked = subprocess.run(["git", "-C", str(self.root), "ls-files"], capture_output=True, text=True, check=True)
        self.assertIn(".agent/notes/existing.md", tracked.stdout)

    def test_invalid_local_paths_and_mixed_backends_fail_before_network(self):
        (self.root / "file").write_text("keep")
        (self.root / "link").symlink_to(self.root, target_is_directory=True)
        for value in ("../outside", str(self.root / "absolute"), ".git/notes", ".shynote-local/notes", "file/sub", "link/notes", "."):
            with self.subTest(value=value):
                code, _, _ = self.run_cli(["init", "--repo", str(self.root), "--notes-dir", value, *self.flags("s3")])
                self.assertEqual(code, 1)
        code, _, _ = self.run_cli(["init", "--repo", str(self.root), *self.flags("s3"), "--parent-page", PARENT])
        self.assertEqual(code, 1)
        self.assertEqual(self.opened, [])
        self.assertFalse((self.root / ".shynote").exists())

    def test_failed_marker_write_rolls_back_local_setup(self):
        original = Path.open
        (self.root / ".gitignore").write_bytes(b"keep\r\n")

        def fail_marker(path, mode="r", *args, **kwargs):
            if path.name == ".shynote" and mode == "x":
                raise PermissionError("Simulated marker write failure")
            return original(path, mode, *args, **kwargs)

        with patch.object(Path, "open", fail_marker):
            code, _, error = self.run_cli(["init", "--repo", str(self.root), *self.flags("s3")])
        self.assertEqual(code, 1)
        self.assertIn("Simulated marker", error)
        self.assertEqual((self.root / ".gitignore").read_bytes(), b"keep\r\n")
        self.assertEqual([p.name for p in self.root.iterdir()], [".gitignore"])

    def test_configured_notes_base_on_both_backends(self):
        for backend in ("s3", "notion"):
            with self.subTest(backend=backend):
                root = self.root / backend
                root.mkdir()
                code, _, error = self.run_cli(["init", "--repo", str(root), *self.flags(backend)])
                self.assertEqual(code, 0, error)
                (root / "hello.md").write_text("outside notes")
                (root / ".agent/notes/hello.md").write_text("inside notes")
                notebook = open_notebook(root / ".agent/notes", s3_client=self.s3, notion_transport=self.notion)
                copy = WorkingCopy(notebook)
                result = copy.transfer("push", ["hello.md"])["results"][0]
                self.assertEqual(result["status"], "created")
                self.assertEqual(notebook.store.read(result["id"]).body, "inside notes")
                self.assertEqual(set(json.loads((root / ".shynote-local/state.json").read_text())["files"]), {"hello.md"})
                new = notebook.store.create("Another", "fetched body", path="sub/fetched.md")
                pulled = copy.transfer("pull", "sub/fetched.md", note_id=new.id)["results"][0]
                self.assertEqual(pulled["status"], "pulled")
                self.assertEqual((root / ".agent/notes/sub/fetched.md").read_text(), "fetched body")
                notebook.config = replace(notebook.config, notes_dir="other")
                with self.assertRaisesRegex(ShyNoteError, "different notebook or notes directory"):
                    WorkingCopy(notebook).transfer("pull", all_files=True)

    def test_missing_notes_dir_is_an_error_without_setup_or_provider_calls(self):
        for backend in ("s3", "notion"):
            with self.subTest(backend=backend):
                marker = (FIXTURES / f"{backend}-repo" / ".shynote").read_text()
                marker = marker.replace('notes_dir = "."\n', '')
                (self.root / ".shynote").write_text(marker)
                for command in (["info"], ["init", *self.flags(backend)], ["push", "hello.md"]):
                    code, result, error = self.run_cli(["--repo", str(self.root), *command])
                    self.assertEqual(code, 1)
                    self.assertIsNone(result)
                    self.assertIn("missing=['notes_dir']", error)
                    self.assertEqual((self.root / ".shynote").read_text(), marker)
                self.assertEqual(list(self.root.iterdir()), [self.root / ".shynote"])
        self.assertEqual(self.opened, [])
        self.assertEqual(self.s3.calls, [])
        self.assertEqual(self.notion.calls, [])

    def test_tracking_without_notes_dir_is_rejected_even_for_explicit_repo_root(self):
        (self.root / ".shynote").write_text((FIXTURES / "s3-repo/.shynote").read_text())
        notebook = open_notebook(self.root, s3_client=self.s3)
        copy = WorkingCopy(notebook)
        (self.root / "hello.md").write_text("hello")
        self.assertEqual(copy.transfer("push", "hello.md")["results"][0]["status"], "created")
        state = json.loads(copy.state_path.read_text())
        self.assertEqual(state["identity"].pop("notes_dir"), ".")
        copy.state_path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ShyNoteError, "different notebook or notes directory"):
            copy.transfer("pull", all_files=True)

    def test_parent_page_url_parsing_rejects_unrelated_urls(self):
        self.assertEqual(_parent_id(PARENT), PARENT)
        self.assertEqual(_parent_id(f"https://team.notion.site/Page-{PARENT.replace('-', '')}?v=abc"), PARENT)
        for value in ("garbage", f"https://example.com/{PARENT}", f"https://notion.com.evil.test/{PARENT}", "https://[", "https://notion.com/no-id"):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                _parent_id(value)


if __name__ == "__main__":
    unittest.main()

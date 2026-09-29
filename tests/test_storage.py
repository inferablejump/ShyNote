from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from shynote.cli import main
from shynote.config import load_config
from shynote.model import (ConfigurationError, Conflict, NotFound, ProviderError,
                           ShyNoteError, UnsupportedCapability)
from shynote.notebook import open_notebook
from shynote.stores.notion import NotionStore, NotionTransport
from shynote.stores.s3 import S3Store
from tests.fakes import FakeNotion, FakeS3


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


class ContractTests(unittest.TestCase):
    """The exact same lifecycle runs through both configured directories."""

    def test_both_backends(self):
        for backend in ("notion", "s3"):
            with self.subTest(backend=backend):
                s3, notion = FakeS3(), FakeNotion()
                notebook = open_notebook(FIXTURES / f"{backend}-repo", s3_client=s3, notion_transport=notion)
                store = notebook.store
                self.assertEqual(s3.calls, [])
                self.assertEqual(notion.calls, [])
                self.assertEqual(store.list_notes(), [])
                body = "# Finding\n\nUnicode: café 日本語\n\n```python\nprint('hello')\n```\n"
                first = store.create("A finding 日本語", body)
                second = store.create("A finding 日本語", "Another note")
                self.assertNotEqual(first.id, second.id)
                self.assertEqual(store.read(first.id).body, body)
                self.assertEqual(store.read(first.id).title, "A finding 日本語")
                s3.calls.clear()
                notion.calls.clear()
                self.assertEqual({n.id for n in store.list_notes()}, {first.id, second.id})
                self.assertFalse(any(call[0] == "GET" for call in s3.calls))
                self.assertFalse(any("/markdown" in call[1] for call in notion.calls))
                with self.assertRaises(ShyNoteError):
                    store.update(first.id, "must not write")
                kwargs = {"revision": first.revision} if store.capabilities.conditional_writes else {"unconditional": True}
                store.update(first.id, "Updated", **kwargs)
                updated = store.read(first.id)
                self.assertEqual(updated.body, "Updated")
                self.assertEqual(updated.title, first.title)
                self.assertNotEqual(updated.revision, first.revision)
                kwargs = {"revision": updated.revision} if store.capabilities.conditional_writes else {"unconditional": True}
                if store.capabilities.archive:
                    store.archive(first.id, **kwargs)
                    self.assertEqual([n.id for n in store.list_notes()], [second.id])
                    retained = store.read(first.id)
                    self.assertTrue(retained.archived)
                    self.assertEqual(retained.body, updated.body)
                    with self.assertRaises(ShyNoteError):
                        store.update(first.id, "resurrection", unconditional=True)
                else:
                    with self.assertRaises(UnsupportedCapability):
                        store.archive(first.id, **kwargs)
                    self.assertEqual(store.read(first.id), updated)
                    self.assertEqual({n.id for n in store.list_notes()}, {first.id, second.id})
                with self.assertRaises(NotFound):
                    store.read(str(uuid4()))
                before = (len(s3.calls), len(notion.calls))
                with self.assertRaises(UnsupportedCapability):
                    notebook.search_content("Finding")
                self.assertEqual(before, (len(s3.calls), len(notion.calls)))

    def test_two_repos_remain_independent_in_one_process(self):
        s3, notion = FakeS3(), FakeNotion()
        a = open_notebook(FIXTURES / "s3-repo", s3_client=s3, notion_transport=notion)
        b = open_notebook(FIXTURES / "notion-repo", s3_client=s3, notion_transport=notion)
        note_a = a.store.create("S3 only", "a")
        note_b = b.store.create("Notion only", "b")
        self.assertEqual([n.title for n in a.store.list_notes()], ["S3 only"])
        self.assertEqual([n.title for n in b.store.list_notes()], ["Notion only"])
        with self.assertRaises(NotFound):
            a.store.read(note_b.id)
        with self.assertRaises(NotFound):
            b.store.read(note_a.id)


class ProviderTests(unittest.TestCase):
    def test_s3_stale_revision_and_race(self):
        client = FakeS3()
        store = open_notebook(FIXTURES / "s3-repo", s3_client=client).store
        note = store.create("Race", "before")
        store.update(note.id, "first winner", revision=note.revision)
        with self.assertRaises(Conflict):
            store.update(note.id, "stale overwrite", revision=note.revision)
        current = store.read(note.id)
        client.before_put = lambda: store.update(note.id, "concurrent winner", revision=current.revision)
        with self.assertRaises(Conflict):
            store.update(note.id, "race loser", revision=current.revision)
        self.assertEqual(store.read(note.id).body, "concurrent winner")

    def test_s3_notebook_prefix_isolation(self):
        client = FakeS3()
        config = load_config(FIXTURES / "s3-repo")
        a, b = S3Store(config, client=client), S3Store(replace(config, notebook="other"), client=client)
        note = a.create("Private", "a")
        self.assertEqual(b.list_notes(), [])
        with self.assertRaises(NotFound):
            b.read(note.id)

    def test_s3_sdk_parameters(self):
        import boto3
        from botocore.stub import ANY, Stubber
        client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
        store = open_notebook(FIXTURES / "s3-repo", s3_client=client).store
        with Stubber(client) as stub:
            stub.add_response("put_object", {"ETag": '"revision"'}, {
                "Bucket": "shynote-test-only", "Key": ANY, "Body": ANY,
                "ContentType": "text/markdown; charset=utf-8", "Metadata": ANY, "IfNoneMatch": "*",
            })
            self.assertEqual(store.create("Title", "Body").revision, '"revision"')
            stub.assert_no_pending_responses()

    def test_s3_auth_error_is_not_missing_note(self):
        from botocore.exceptions import ClientError
        client = FakeS3()
        store = open_notebook(FIXTURES / "s3-repo", s3_client=client).store
        with patch.object(client, "get_object", side_effect=ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")):
            with self.assertRaises(ProviderError):
                store.read(str(uuid4()))

    def test_s3_missing_credentials_and_list_failure_are_actionable(self):
        from botocore.exceptions import ClientError, NoCredentialsError
        client = FakeS3()
        store = open_notebook(FIXTURES / "s3-repo", s3_client=client).store
        with patch.object(client, "get_object", side_effect=NoCredentialsError()):
            with self.assertRaisesRegex(ProviderError, "NoCredentialsError"):
                store.read(str(uuid4()))
        with patch.object(client, "paginate", side_effect=ClientError({"Error": {"Code": "AccessDenied"}}, "ListObjectsV2")):
            with self.assertRaisesRegex(ProviderError, "AccessDenied"):
                store.list_notes()

    def test_notion_rejects_atomic_write_before_network(self):
        transport = FakeNotion()
        store = open_notebook(FIXTURES / "notion-repo", notion_transport=transport).store
        with self.assertRaises(UnsupportedCapability):
            store.update(str(uuid4()), "body", revision="revision")
        self.assertEqual(transport.calls, [])

    def test_notion_parent_isolation(self):
        transport = FakeNotion()
        config = load_config(FIXTURES / "notion-repo")
        a = NotionStore(config, transport=transport)
        other = replace(config, storage=replace(config.storage, parent_page_id=str(uuid4())))
        b = NotionStore(other, transport=transport)
        note = a.create("Private", "a")
        with self.assertRaises(NotFound):
            b.update(note.id, "overwrite", unconditional=True)
        self.assertEqual(b.list_notes(), [])
        self.assertEqual(a.read(note.id).body, "a")

    def test_notion_partial_markdown_is_not_returned(self):
        transport = FakeNotion()
        store = open_notebook(FIXTURES / "notion-repo", notion_transport=transport).store
        note = store.create("Title", "Body")
        transport.truncated = True
        with self.assertRaises(ProviderError):
            store.read(note.id)

    def test_notion_transport_headers_and_missing_credentials(self):
        transport = NotionTransport("SHYNOTE_TEST_NOTION_TOKEN")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ProviderError), patch("shynote.stores.notion.urlopen") as request:
                transport.request("GET", "pages/test")
            request.assert_not_called()
        with patch.dict(os.environ, {"SHYNOTE_TEST_NOTION_TOKEN": "fixture-token"}):
            with patch("shynote.stores.notion.urlopen") as request:
                request.return_value.__enter__.return_value = StringIO('{"ok": true}')
                self.assertEqual(transport.request("GET", "pages/test"), {"ok": True})
                sent = request.call_args.args[0]
                self.assertEqual(sent.full_url, "https://api.notion.com/v1/pages/test")
                self.assertEqual(sent.get_header("Authorization"), "Bearer fixture-token")
                self.assertEqual(sent.get_header("Notion-version"), "2026-03-11")


class ConfigurationTests(unittest.TestCase):
    def test_invalid_configs(self):
        original = (FIXTURES / "s3-repo" / ".shynote").read_text()
        invalid = [
            original.replace('backend = "s3"', 'backend = ["s3", "notion"]'),
            original + '\n[notion]\nparent_page_id = "extra"\n',
            original + '\nsecret_access_key = "no-secrets-here"\n',
            original.replace('backend = "s3"', 'backend = "unknown"'),
            original.replace('version = 1', 'version = true'),
            original.replace('prefix = "fixtures"', 'prefix = "../escape"'),
            original.replace('prefix = "fixtures"', 'prefix = "/"'),
            original + '\nendpoint_url = "http://example.com"\n',
            original + '\nendpoint_url = "https://user:password@example.com"\n',
        ]
        for content in invalid:
            with self.subTest(content=content), TemporaryDirectory() as directory:
                (Path(directory) / ".shynote").write_text(content)
                with self.assertRaises(ConfigurationError):
                    load_config(directory)

    def test_nested_directory_discovery(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".shynote").write_text((FIXTURES / "notion-repo" / ".shynote").read_text())
            nested = root / "src" / "nested"
            nested.mkdir(parents=True)
            self.assertEqual(load_config(nested).root, root)

    def test_info_from_both_dummy_working_directories(self):
        for backend in ("s3", "notion"):
            with self.subTest(backend=backend):
                env = {**os.environ, "PYTHONPATH": str(ROOT)}
                env.pop("SHYNOTE_TEST_NOTION_TOKEN", None)
                process = subprocess.run([sys.executable, "-m", "shynote", "info"],
                                         cwd=FIXTURES / f"{backend}-repo", env=env,
                                         capture_output=True, text=True, timeout=15)
                self.assertEqual(process.returncode, 0, process.stderr)
                info = json.loads(process.stdout)
                self.assertEqual(info["backend"], backend)
                self.assertTrue(info["capabilities"]["title_search"])
                self.assertFalse(info["capabilities"]["content_search"])
                self.assertEqual(info["capabilities"]["archive"], backend == "s3")

    def test_notion_archive_is_unsupported_without_provider_calls(self):
        transport = FakeNotion()
        notebook = open_notebook(FIXTURES / "notion-repo", notion_transport=transport)
        note = notebook.store.create("Retained", "Keep this content")
        transport.calls.clear()
        for guard in ({}, {"revision": note.revision}, {"unconditional": True}):
            with self.subTest(guard=guard):
                with self.assertRaisesRegex(UnsupportedCapability, "Archive is not supported by the notion backend"):
                    notebook.store.archive(note.id, **guard)
                self.assertEqual(transport.calls, [])
        self.assertEqual(notebook.store.read(note.id), note)
        self.assertEqual([n.id for n in notebook.store.search_title("Retained")], [note.id])

    def test_cli_notion_archive_returns_unsupported_without_credentials(self):
        with patch.dict(os.environ):
            os.environ.pop("SHYNOTE_TEST_NOTION_TOKEN", None)
            with redirect_stdout(StringIO()) as out, redirect_stderr(StringIO()) as err:
                result = main(["--repo", str(FIXTURES / "notion-repo"), "archive", str(uuid4()), "--unconditional"])
        self.assertEqual(result, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("Archive is not supported by the notion backend", err.getvalue())

    def test_cli_search_returns_honest_error(self):
        for backend, command in (("notion", "search-content"), ("s3", "search-content")):
            with self.subTest(backend=backend, command=command):
                with redirect_stdout(StringIO()) as out, redirect_stderr(StringIO()) as err:
                    result = main(["--repo", str(FIXTURES / f"{backend}-repo"), command, "race"])
                self.assertEqual(result, 1)
                self.assertEqual(out.getvalue(), "")
                self.assertIn(f"not supported by the {backend} backend", err.getvalue())


class SearchTests(unittest.TestCase):
    def test_unsupported_search_does_not_contact_providers(self):
        for backend in ("notion", "s3"):
            with self.subTest(backend=backend):
                s3, notion = FakeS3(), FakeNotion()
                notebook = open_notebook(FIXTURES / f"{backend}-repo", s3_client=s3, notion_transport=notion)
                for target in (notebook, notebook.store):
                    with self.assertRaises(UnsupportedCapability):
                        target.search_content("finding")
                self.assertEqual(s3.calls, [])
                self.assertEqual(notion.calls, [])

    def test_s3_title_search_scopes_paginates_and_never_fetches_bodies(self):
        client = FakeS3()
        notebook = open_notebook(FIXTURES / "s3-repo", s3_client=client)
        other = S3Store(replace(notebook.config, notebook="other"), client=client)
        other.create("Cache outside notebook", "private")
        first = notebook.store.create("Cache finding 日本語", "body")
        notebook.store.create("Other finding", "Cache only in body")
        archived = notebook.store.create("Cache archived", "body")
        notebook.store.archive(archived.id, revision=archived.revision)
        second = notebook.store.create("Cache finding 日本語", "duplicate title")
        client.calls.clear()
        matches = notebook.search_title(" cAcHe ")
        self.assertEqual([n.id for n in matches], [first.id, second.id])
        self.assertTrue(all(not n.archived for n in matches))
        self.assertEqual(sum(method == "HEAD" for method, _ in client.calls), 4)
        self.assertTrue(all(method in {"LIST", "HEAD"} for method, _ in client.calls))
        self.assertTrue(all(key.startswith(notebook.store.prefix) for _, key in client.calls))
        self.assertEqual([n.id for n in notebook.search_title("日本語")], [first.id, second.id])
        self.assertEqual(notebook.search_title("absent"), [])

    def test_s3_empty_title_query_does_not_list_objects(self):
        client = FakeS3()
        notebook = open_notebook(FIXTURES / "s3-repo", s3_client=client)
        with self.assertRaises(ShyNoteError):
            notebook.search_title(" \n ")
        self.assertEqual(client.calls, [])

    def test_notion_title_search_scopes_paginates_and_never_fetches_bodies(self):
        transport = FakeNotion()
        notebook = open_notebook(FIXTURES / "notion-repo", notion_transport=transport)
        other_config = replace(notebook.config, storage=replace(notebook.config.storage, parent_page_id=str(uuid4())))
        other = NotionStore(other_config, transport=transport)
        other.create("Cache outside notebook", "private")  # First result page is filtered out.
        first = notebook.store.create("Cache finding 日本語", "body")
        notebook.store.create("Other finding", "Cache only in body")
        archived = notebook.store.create("Cache archived", "body")
        transport.pages[archived.id]["in_trash"] = True  # Trashed outside ShyNote.
        second = notebook.store.create("Cache finding 日本語", "duplicate title")
        nested_config = replace(notebook.config, storage=replace(notebook.config.storage, parent_page_id=first.id))
        NotionStore(nested_config, transport=transport).create("Cache nested page", "not a direct note")
        transport.calls.clear()
        matches = notebook.search_title(" Cache ")
        self.assertEqual([n.id for n in matches], [first.id, second.id])
        self.assertTrue(all(not n.archived for n in matches))
        self.assertGreater(len(transport.calls), 1)
        self.assertTrue(all(method == "POST" and path == "search" for method, path, _ in transport.calls))
        self.assertTrue(all(payload["query"] == "Cache" for _, _, payload in transport.calls))
        self.assertEqual(notebook.search_title("absent"), [])

    def test_notion_empty_query_is_not_a_workspace_scan(self):
        transport = FakeNotion()
        notebook = open_notebook(FIXTURES / "notion-repo", notion_transport=transport)
        with self.assertRaises(ShyNoteError):
            notebook.search_title(" \n ")
        self.assertEqual(transport.calls, [])

    def test_notion_incomplete_results_and_broken_pagination_fail(self):
        notebook = open_notebook(FIXTURES / "notion-repo", notion_transport=FakeNotion())
        for result in (
            {"results": [], "request_status": {"type": "incomplete"}},
            {"results": [], "has_more": True, "next_cursor": None},
            {"results": [], "has_more": True, "next_cursor": "repeated"},
        ):
            with self.subTest(result=result):
                with patch.object(notebook.store.transport, "request", return_value=result):
                    with self.assertRaises(ProviderError):
                        notebook.search_title("test")

    def test_cli_title_search_serializes_matches_and_propagates_provider_errors(self):
        notebook = open_notebook(FIXTURES / "notion-repo", notion_transport=FakeNotion())
        note = notebook.store.create("Finding", "body")
        with patch("shynote.cli.open_notebook", return_value=notebook):
            with redirect_stdout(StringIO()) as out:
                self.assertEqual(main(["search-title", "Finding"]), 0)
            self.assertEqual(json.loads(out.getvalue()), [{"id": note.id, "title": "Finding", "archived": False}])
            with patch.object(notebook.store.transport, "request", side_effect=ProviderError("Unauthorized")):
                with redirect_stdout(StringIO()) as out, redirect_stderr(StringIO()) as err:
                    self.assertEqual(main(["search-title", "Finding"]), 1)
                self.assertEqual(out.getvalue(), "")
                self.assertIn("Unauthorized", err.getvalue())


if __name__ == "__main__":
    unittest.main()

from contextlib import redirect_stderr
from dataclasses import replace
from io import BytesIO, StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from shynote.model import ProviderError
from shynote.notebook import Notebook, open_notebook
from shynote.stores.notion import NotionStore, NotionTransport
from shynote.working_copy import WorkingCopy
from tests.fakes import FakeNotion

FIXTURE = Path(__file__).parent / "fixtures/notion-repo"


def http_error(status=429, retry_after="12", body=None):
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return HTTPError("https://api.notion.com/v1/pages", status, "fixture", headers,
                     BytesIO(json.dumps(body or {}).encode()))


class NotionRateLimitTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict("os.environ", {"SHYNOTE_TEST_TOKEN": "fixture-secret"}))
        self.sleep = self.enterContext(patch("shynote.stores.notion.time.sleep"))
        self.enterContext(patch("shynote.stores.notion.random.uniform", return_value=0))
        self.stderr = self.enterContext(redirect_stderr(StringIO()))
        self.transport = NotionTransport("SHYNOTE_TEST_TOKEN")

    def test_get_post_and_patch_retry_same_request_after_retry_after(self):
        for method in ("GET", "POST", "PATCH"):
            with self.subTest(method=method):
                self.sleep.reset_mock()
                with patch("shynote.stores.notion.urlopen", side_effect=[
                    http_error(), StringIO('{"ok": true}')
                ]) as request:
                    self.assertEqual(self.transport.request(method, "pages", {"markdown": "body"}), {"ok": True})
                self.assertEqual(request.call_count, 2)
                self.assertIs(request.call_args_list[0].args[0], request.call_args_list[1].args[0])
                self.sleep.assert_called_once_with(12)
        self.assertIn("waiting 12.0s", self.stderr.getvalue())
        self.assertNotIn("fixture-secret", self.stderr.getvalue())

    def test_missing_invalid_or_short_header_uses_bounded_backoff(self):
        errors = [http_error(retry_after=value) for value in (None, "invalid", "-1", "0")]
        with patch("shynote.stores.notion.urlopen", side_effect=errors + [StringIO('{}')]):
            self.transport.request("GET", "pages/test")
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [1, 2, 4, 8])

    def test_attempt_and_total_wait_limits_never_retry_before_retry_after(self):
        with patch("shynote.stores.notion.urlopen", side_effect=[http_error() for _ in range(5)]) as request:
            with self.assertRaisesRegex(ProviderError, "Automatic retry limit reached"):
                self.transport.request("GET", "pages/test")
        self.assertEqual(request.call_count, 5)
        self.assertEqual(self.sleep.call_count, 4)
        self.sleep.reset_mock()
        with patch("shynote.stores.notion.urlopen", side_effect=[http_error(retry_after="70") for _ in range(2)]) as request:
            with self.assertRaisesRegex(ProviderError, "Retry after 70"):
                self.transport.request("POST", "pages", {})
        self.assertEqual(request.call_count, 2)
        self.sleep.assert_called_once_with(70)
        self.sleep.reset_mock()
        with patch("shynote.stores.notion.urlopen", side_effect=http_error(retry_after="121")) as request:
            with self.assertRaises(ProviderError):
                self.transport.request("GET", "pages/test")
        self.assertEqual(request.call_count, 1)
        self.sleep.assert_not_called()

    def test_restricted_access_and_ambiguous_writes_are_not_retried(self):
        blocked = {"additional_data": {"rate_limit_reason": "public_api_request_blocked"}}
        for error in (http_error(body=blocked), http_error(401), http_error(403),
                      http_error(500), http_error(503), URLError("connection lost")):
            with self.subTest(error=error), patch("shynote.stores.notion.urlopen", side_effect=error) as request:
                with self.assertRaises(ProviderError):
                    self.transport.request("POST", "pages", {})
                self.assertEqual(request.call_count, 1)
        self.sleep.assert_not_called()

    def test_rate_limit_during_readback_retries_read_without_recreating_page(self):
        fake = FakeNotion()
        store = NotionStore(open_notebook(FIXTURE).config, transport=self.transport)
        throttled = False

        def urlopen(request, timeout):
            nonlocal throttled
            route = request.full_url.split("/v1/", 1)[1]
            if route.endswith("/markdown") and not throttled:
                throttled = True
                raise http_error()
            payload = json.loads(request.data) if request.data else None
            response = fake.request(request.method, route, payload)
            return StringIO(json.dumps(response))

        with TemporaryDirectory() as directory, patch("shynote.stores.notion.urlopen", side_effect=urlopen):
            root = Path(directory)
            (root / "note.md").write_text("body")
            copy = WorkingCopy(Notebook(replace(open_notebook(FIXTURE).config,
                                               root=root, notes_dir="."), store))
            result = copy.transfer("push", "note.md")["results"][0]
            self.assertEqual(result["status"], "created")
            self.assertEqual(len(fake.pages), 1)
            self.assertEqual(sum(method == "POST" and route == "pages" for method, route, _ in fake.calls), 1)
            self.assertEqual(json.loads(copy.state_path.read_text())["files"]["note.md"]["id"], result["id"])
        self.sleep.assert_called_once_with(12)

    def test_exhausted_readback_reports_created_id_and_does_not_advance_tracking(self):
        fake = FakeNotion()
        notebook = open_notebook(FIXTURE, notion_transport=fake)
        request = fake.request

        def fail_readback(method, route, payload=None):
            if route.endswith("/markdown"):
                raise ProviderError("Notion request failed (HTTP 429). Automatic retry limit reached.")
            return request(method, route, payload)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "note.md").write_text("body")
            copy = WorkingCopy(Notebook(replace(notebook.config, root=root, notes_dir="."), notebook.store))
            with patch.object(fake, "request", side_effect=fail_readback):
                result = copy.transfer("push", "note.md")["results"][0]
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["id"], next(iter(fake.pages)))
            self.assertIn("pull --id", result["error"])
            self.assertFalse(copy.state_path.exists())
            restored = copy.transfer("pull", note_id=result["id"])["results"][0]
            self.assertEqual(restored["status"], "pulled")
            self.assertEqual(len(fake.pages), 1)


if __name__ == "__main__":
    unittest.main()

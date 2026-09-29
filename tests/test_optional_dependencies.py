"""Exercise the shared CLI without loading any third-party site packages."""
import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


class OptionalDependencyTests(unittest.TestCase):
    def run_without_sdk(self, backend, command):
        return subprocess.run(
            [sys.executable, "-S", "-m", "shynote", "--repo",
             str(ROOT / "tests" / "fixtures" / f"{backend}-repo"), command],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )

    def test_info_works_without_backend_dependencies(self):
        for backend in ("s3", "notion"):
            with self.subTest(backend=backend):
                result = self.run_without_sdk(backend, "info")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["backend"], backend)

    def test_s3_operation_reports_missing_extra_without_traceback(self):
        result = self.run_without_sdk("s3", "list")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("shynote[s3]", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()

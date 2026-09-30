"""Opt-in Notion CLI test. Creates synthetic notes under a supplied test parent."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from uuid import UUID, uuid4

from shynote.stores.notion import NotionTransport


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-page-id", required=True, type=UUID)
    parser.add_argument("--token-env", default="NOTION_TOKEN")
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    parent = str(args.parent_page_id)
    run_id = uuid4().hex[:12]
    note_path = f"hierarchy-{run_id}/design/finding.md"
    report = {"started_at": datetime.now(timezone.utc).isoformat(),
              "parent_page_id": parent, "run_id": run_id, "checks": [],
              "created_notes": [], "created_directories": [], "passed": False,
              "cleanup": "Notes and directories are retained for manual cleanup; Notion archive is unsupported."}
    source = str(Path(__file__).resolve().parents[1])

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        report["checks"].append(name)
        print(f"PASS: {name}", flush=True)

    def cli(repo, *arguments, expect=0):
        # Space CLI invocations to keep this small live test below sustained limits.
        time.sleep(1)
        result = subprocess.run(
            [sys.executable, "-m", "shynote", *arguments], cwd=repo,
            env={**os.environ, "PYTHONPATH": source}, capture_output=True, text=True, timeout=90)
        output = json.loads(result.stdout) if result.stdout else result.stderr.strip()
        if result.returncode != expect:
            raise RuntimeError(f"CLI {arguments[0]} exited {result.returncode}: {output}")
        return output

    try:
        transport = NotionTransport(args.token_env)
        page = transport.request("GET", f"pages/{parent}")
        check("test parent accessible and active", not page.get("in_trash") and not page.get("archived"))
        with tempfile.TemporaryDirectory(prefix="shynote-live-notion-") as directory:
            first, second = Path(directory) / "first", Path(directory) / "second"
            marker = (f'version = 1\nnotebook = "live-{run_id}"\nbackend = "notion"\nnotes_dir = "."\n'
                      f'[storage]\nparent_page_id = "{parent}"\ntoken_env = "{args.token_env}"\n')
            for repo in (first, second):
                repo.mkdir()
                (repo / ".shynote").write_text(marker)
            info = cli(first, "info")
            check("Notion capabilities", info["backend"] == "notion" and
                  info["capabilities"] == {"conditional_writes": False, "title_search": True,
                                           "content_search": False, "archive": False})
            initial_ids = {item["id"] for item in cli(first, "list")}
            local = first / note_path
            local.parent.mkdir(parents=True)
            local.write_text("# Live finding\n\nOriginal finding: café 日本語.\n\n```python\nprint('hello')\n```\n", encoding="utf-8")
            title = f"ShyNote live {run_id} finding"
            preview = cli(first, "push", note_path, "--title", title, "--dry-run")["results"][0]
            check("new push dry run", preview["status"] == "would_create" and "Original finding" in preview["diff"]
                  and not (first / ".shynote-local").exists())
            check("new dry run creates no remote note", {item["id"] for item in cli(first, "list")} == initial_ids)
            created = cli(first, "push", note_path, "--title", title)["results"][0]
            note_id = created["id"]
            report["created_notes"].append({"id": note_id, "title": title})
            check("first push creates and tracks note", created["status"] == "created")
            note_page = transport.request("GET", f"pages/{note_id}")
            folder_id = note_page["parent"]["page_id"]
            report["created_directories"].append(folder_id)
            folder_page = transport.request("GET", f"pages/{folder_id}")
            top_id = folder_page["parent"]["page_id"]
            report["created_directories"].append(top_id)
            top_page = transport.request("GET", f"pages/{top_id}")
            check("note resides in nested directory pages", top_page["parent"]["page_id"] == parent
                  and folder_id != top_id and folder_id != parent)
            fetched = cli(first, "read", note_id)
            check("Markdown title Unicode and code survive", fetched["title"] == title and
                  "café 日本語" in fetched["body"] and "print('hello')" in fetched["body"])
            report["creation_normalized_markdown"] = fetched["body"] != local.read_text(encoding="utf-8")
            report["created_markdown"] = fetched["body"]
            check("active list contains note", note_id in {item["id"] for item in cli(first, "list")})
            for attempt in range(15):
                matches = cli(first, "search-title", run_id)
                if note_id in {item["id"] for item in matches}:
                    break
                time.sleep(2)
            check("title search finds created note", note_id in {item["id"] for item in matches})
            check("content search explicitly unsupported", "not supported" in cli(first, "search-content", run_id, expect=1))
            check("archive explicitly unsupported", "not supported" in cli(first, "archive", note_id, "--unconditional", expect=1))
            check("unsupported archive retains note", not cli(first, "read", note_id)["archived"])
            restored = cli(second, "pull", "--id", note_id)["results"][0]
            check("fresh checkout restores saved path", restored["status"] == "pulled" and restored["file"] == note_path
                  and (second / note_path).read_text(encoding="utf-8") == fetched["body"])
            submitted_body = local.read_text(encoding="utf-8").replace("Original finding", "Updated finding")
            local.write_text(submitted_body, encoding="utf-8")
            state = (first / ".shynote-local/state.json").read_bytes()
            refused = cli(first, "push", note_path, expect=1)["results"][0]
            check("push requires unconditional opt-in", refused["status"] == "error" and "--unconditional" in refused["error"])
            preview = cli(first, "push", note_path, "--unconditional", "--dry-run")["results"][0]
            check("push dry run shows diff", preview["status"] == "would_push" and "Updated finding" in preview["diff"])
            check("push dry run preserves remote and state", cli(first, "read", note_id)["body"] == fetched["body"]
                  and (first / ".shynote-local/state.json").read_bytes() == state)
            pushed = cli(first, "push", note_path, "--unconditional", "--diff")["results"][0]
            updated_body = cli(first, "read", note_id)["body"]
            report["updated_markdown"] = updated_body
            check("push persists ordinary Markdown update", pushed["status"] == "pushed" and
                  "Updated finding: café 日本語." in updated_body and "Original finding" not in updated_body
                  and "```python\nprint('hello')\n```" in updated_body)
            report["update_normalized_markdown"] = updated_body != submitted_body
            check("normalized push preserves local formatting", local.read_text(encoding="utf-8") == submitted_body)
            check("normalized push remains synchronized", cli(first, "push", note_path, "--unconditional")["results"][0]["status"] == "unchanged")
            second_file = second / note_path
            second_state = (second / ".shynote-local/state.json").read_bytes()
            preview = cli(second, "pull", "--all", "--dry-run")["results"][0]
            check("pull dry run preserves local file and state", preview["status"] == "would_pull" and preview["diff"]
                  and second_file.read_text(encoding="utf-8") == fetched["body"]
                  and (second / ".shynote-local/state.json").read_bytes() == second_state)
            second_file.write_text(fetched["body"].replace("Original finding", "Conflicting local finding"), encoding="utf-8")
            for operation in ("push", "pull"):
                flags = ["--unconditional"] if operation == "push" else []
                result = cli(second, operation, note_path, *flags, expect=1)["results"][0]
                check(f"divergent {operation} rejected", result["status"] == "conflict")
            check("conflict preserves remote and local", cli(first, "read", note_id)["body"] == updated_body and
                  "Conflicting local finding" in second_file.read_text(encoding="utf-8"))
            second_file.write_text(fetched["body"], encoding="utf-8")
            pulled = cli(second, "pull", "--all", "--diff")["results"][0]
            check("pull all applies safe remote update with diff", pulled["status"] == "pulled" and pulled["diff"] and
                  second_file.read_text(encoding="utf-8") == updated_body)
            (second / "scratch.md").write_text("A new note discovered by push all.")
            second_file.write_text(updated_body.replace("Updated finding", "Bulk updated finding"), encoding="utf-8")
            bulk = cli(second, "push", "--all", "--unconditional")["results"]
            for result in bulk:
                if result["status"] == "created":
                    report["created_notes"].append({"id": result["id"], "title": result["title"]})
            check("push all updates tracked and creates new files",
                  [item["file"] for item in bulk] == [note_path, "scratch.md"] and
                  [item["status"] for item in bulk] == ["pushed", "created"])
            second_file.unlink()
            check("missing local file does not delete remote", cli(second, "push", "--all", "--unconditional")["results"][0]["status"] == "skipped_missing"
                  and not cli(first, "read", note_id)["archived"])
            sibling_path = f"hierarchy-{run_id}/design/another.md"
            (second / sibling_path).write_text("A sibling note to verify directory reuse.")
            sibling = cli(second, "push", sibling_path)["results"][0]
            report["created_notes"].append({"id": sibling["id"], "title": sibling["title"]})
            sibling_page = transport.request("GET", f"pages/{sibling['id']}")
            check("sibling note reuses existing directories", sibling_page["parent"]["page_id"] == folder_id)
            listing = cli(first, "list")
            check("list includes nested paths and excludes directory pages",
                  any(item["id"] == sibling["id"] and item["path"] == sibling_path for item in listing)
                  and not {folder_id, top_id}.intersection(item["id"] for item in listing))
            report["passed"] = True
    except Exception as exc:
        report["error"] = str(exc)
        print(f"FAIL: {exc}", flush=True)
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Retained {len(report['created_notes'])} test note(s); report: {args.report}", flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

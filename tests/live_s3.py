"""Create a private temporary S3 bucket, exercise the CLI, and remove test resources."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

import boto3
from botocore.config import Config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    session = boto3.Session(region_name=args.region)
    settings = Config(connect_timeout=5, read_timeout=20,
                      retries={"mode": "standard", "total_max_attempts": 2})
    identity = session.client("sts", region_name=args.region, config=settings).get_caller_identity()
    client = session.client("s3", region_name=args.region, config=settings)
    bucket = f"shynote-live-{uuid4().hex}"
    notebook_id = str(uuid4())
    prefix = f"smoke/{notebook_id}/"
    owner = identity["Account"]
    report = {"started_at": datetime.now(timezone.utc).isoformat(),
              "region": args.region, "bucket": bucket, "identity": identity["Arn"],
              "credential_source": session.get_credentials().method,
              "checks": [], "passed": False, "cleanup_complete": False}
    created = False

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        report["checks"].append(name)
        print(f"PASS: {name}", flush=True)

    def cli(repo, *arguments, expect=0):
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        result = subprocess.run([sys.executable, "-m", "shynote", *arguments], cwd=repo,
                                env=env, capture_output=True, text=True, timeout=90)
        if result.returncode != expect:
            raise RuntimeError(f"CLI {arguments[0]} exited {result.returncode}: {result.stderr.strip()}")
        return json.loads(result.stdout) if result.stdout else result.stderr

    print(f"Identity: {identity['Arn']} ({report['credential_source']})", flush=True)
    print(f"Temporary bucket: {bucket} ({args.region})", flush=True)
    try:
        create_args = {"Bucket": bucket}
        if args.region != "us-east-1":
            create_args["CreateBucketConfiguration"] = {"LocationConstraint": args.region}
        client.create_bucket(**create_args)
        created = True
        client.put_public_access_block(Bucket=bucket, ExpectedBucketOwner=owner,
                                      PublicAccessBlockConfiguration={
                                          "BlockPublicAcls": True, "IgnorePublicAcls": True,
                                          "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
        client.put_bucket_encryption(Bucket=bucket, ExpectedBucketOwner=owner,
                                     ServerSideEncryptionConfiguration={"Rules": [
                                         {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
        with TemporaryDirectory(prefix="shynote-live-s3-") as directory:
            repo = Path(directory) / "first-checkout"
            repo.mkdir()
            marker = (
                f'version = 1\nnotebook = "{notebook_id}"\nbackend = "s3"\nnotes_dir = "."\n\n'
                f'[storage]\nbucket = "{bucket}"\nprefix = "smoke"\nregion = "{args.region}"\n'
            )
            (repo / ".shynote").write_text(marker)
            body = "# S3 live finding\n\nTitle search reads metadata, not note bodies.\n\nUnicode: café 日本語\n"
            (repo / "finding.md").write_text(body, encoding="utf-8")
            info = cli(repo, "info")
            check("configured S3 notebook", info["backend"] == "s3" and info["notebook"] == notebook_id)
            check("empty notebook listing", cli(repo, "list") == [])
            created_note = cli(repo, "push", "finding.md", "--title", "S3 metadata finding 日本語")["results"][0]
            check("first push creates and tracks note", created_note["status"] == "created" and
                  cli(repo, "push", "finding.md")["results"][0]["status"] == "unchanged")
            note = cli(repo, "read", created_note["id"])
            check("push and read exact Markdown", note["body"] == body)
            check("title search", [n["id"] for n in cli(repo, "search-title", "METADATA")] == [note["id"]])
            check("content search reports unsupported",
                  "not supported by the s3 backend" in cli(repo, "search-content", "metadata", expect=1))
            second = Path(directory) / "second-checkout"
            second.mkdir()
            (second / ".shynote").write_text(marker)
            check("fresh checkout reads persisted note", cli(second, "read", note["id"])["body"] == body)
            check("pull into fresh checkout", cli(second, "pull", "finding.md", "--id", note["id"])["results"][0]["status"] == "pulled")
            (second / ".shynote").write_text(marker.replace(notebook_id, str(uuid4())))
            check("different notebook is isolated", cli(second, "list") == [])
            (second / ".shynote").write_text(marker)
            (repo / "finding.md").write_text("Updated finding: conditional writes reject stale revisions.\n")
            check("push updates tracked note", cli(repo, "push", "finding.md")["results"][0]["status"] == "pushed")
            updated = cli(repo, "read", note["id"])
            check("update persists", updated["body"].startswith("Updated finding:"))
            (second / "finding.md").write_text("Stale checkout edit must not overwrite the remote note.\n")
            error = cli(second, "push", "finding.md", expect=1)
            check("divergent push rejected", error["results"][0]["status"] == "conflict")
            check("rejected update preserves revision", cli(repo, "read", note["id"])["revision"] == updated["revision"])
            cli(repo, "archive", note["id"], "--revision", updated["revision"])
            check("archive hides active note", cli(repo, "list") == [] and cli(repo, "search-title", "metadata") == [])
            check("archive retains readable content", cli(repo, "read", note["id"])["archived"])
            # Exercise S3's condition directly with the real adapter. A race that
            # occurs after its preflight read must still fail at the server.
            from shynote import open_notebook
            from shynote.model import Conflict
            store = open_notebook(repo).store
            try:
                store._put(store.read(note["id"]), IfMatch=note["revision"])
            except Conflict:
                check("S3 rejects stale conditional write at server", True)
            else:
                raise AssertionError("S3 accepted a stale ETag")
            report["passed"] = True
    finally:
        try:
            if created:
                paginator = client.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=bucket, Prefix=prefix, ExpectedBucketOwner=owner):
                    for item in page.get("Contents", []):
                        client.delete_object(Bucket=bucket, Key=item["Key"], ExpectedBucketOwner=owner)
                client.delete_bucket(Bucket=bucket, ExpectedBucketOwner=owner)
                report["cleanup_complete"] = True
                print("Removed test objects and temporary bucket.", flush=True)
        finally:
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(f"All {len(report['checks'])} live checks passed.", flush=True)


if __name__ == "__main__":
    main()

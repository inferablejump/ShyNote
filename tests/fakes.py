"""Stateful service doubles. They replace transports, never the real adapters."""
from copy import deepcopy
import hashlib
from io import BytesIO
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from botocore.exceptions import ClientError

from shynote.model import NotFound


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.before_put = None

    def error(self, code, operation):
        raise ClientError({"Error": {"Code": code, "Message": "fixture error"}}, operation)

    def put_object(self, **args):
        self.calls.append(("PUT", args["Key"]))
        key = (args["Bucket"], args["Key"])
        if self.before_put:
            hook, self.before_put = self.before_put, None
            hook()
        current = self.objects.get(key)
        if args.get("IfNoneMatch") == "*" and current:
            self.error("PreconditionFailed", "PutObject")
        if "IfMatch" in args and (not current or current["ETag"] != args["IfMatch"]):
            self.error("PreconditionFailed", "PutObject")
        etag = '"' + hashlib.sha256(args["Body"]).hexdigest() + '"'
        self.objects[key] = {"Body": args["Body"], "Metadata": args["Metadata"], "ETag": etag}
        return {"ETag": etag}

    def get_object(self, **args):
        self.calls.append(("GET", args["Key"]))
        item = self.objects.get((args["Bucket"], args["Key"]))
        if not item:
            self.error("NoSuchKey", "GetObject")
        return {**item, "Body": BytesIO(item["Body"])}

    def head_object(self, **args):
        self.calls.append(("HEAD", args["Key"]))
        item = self.objects.get((args["Bucket"], args["Key"]))
        if not item:
            self.error("404", "HeadObject")
        return {"Metadata": item["Metadata"], "ETag": item["ETag"]}

    def get_paginator(self, operation):
        assert operation == "list_objects_v2"
        return self

    def list_objects_v2(self, **args):
        self.calls.append(("LIST", args["Prefix"]))
        keys = [key for bucket, key in self.objects
                if bucket == args["Bucket"] and key.startswith(args["Prefix"])]
        return {"Contents": [{"Key": key} for key in keys[:args["MaxKeys"]]]}

    def paginate(self, **args):
        self.calls.append(("LIST", args["Prefix"]))
        keys = [key for bucket, key in self.objects
                if bucket == args["Bucket"] and key.startswith(args["Prefix"])]
        for key in keys:
            yield {"Contents": [{"Key": key}]}  # Force pagination.


class FakeNotion:
    def __init__(self):
        self.pages = {}
        self.bodies = {}
        self.calls = []
        self.clock = 0
        self.truncated = False
        self.normalize_markdown = lambda body: body

    def revision(self):
        self.clock += 1
        return f"revision-{self.clock}"

    def request(self, method, path, payload=None):
        self.calls.append((method, path, deepcopy(payload)))
        route = urlsplit(path)
        parts = route.path.split("/")
        if method == "POST" and path == "search":
            assert payload["filter"] == {"property": "object", "value": "page"}
            query = payload["query"].casefold()
            pages = [p for p in self.pages.values()
                     if query in p["properties"]["title"]["title"][0]["text"]["content"].casefold()]
            start = int(payload.get("start_cursor", "0"))
            more = start + 1 < len(pages)
            return {"results": deepcopy(pages[start:start + 1]), "has_more": more,
                    "next_cursor": str(start + 1) if more else None}
        if method == "POST" and path == "pages":
            note_id = str(uuid4())
            page = {"object": "page", "id": note_id, "parent": payload["parent"],
                    "properties": payload["properties"], "last_edited_time": self.revision(),
                    "in_trash": False}
            self.pages[note_id] = page
            self.bodies[note_id] = self.normalize_markdown(payload["markdown"])
            return deepcopy(page)
        if parts[0] == "blocks":
            assert method == "GET" and parts[2] == "children"
            if parts[1] in self.bodies:
                body = self.bodies[parts[1]]
                if body.startswith("```") and "\n```" in body:
                    content = body.split("\n", 1)[1].split("\n```", 1)[0]
                    return {"results": [{"type": "code", "code": {
                        "rich_text": [{"plain_text": content}]}}], "has_more": False}
                return {"results": [{"type": "paragraph"}], "has_more": False}
            pages = [p for p in self.pages.values() if p["parent"]["page_id"] == parts[1]]
            start = int(parse_qs(route.query).get("start_cursor", ["0"])[0])
            batch = pages[start:start + 1]
            results = [{"id": p["id"], "type": "child_page", "in_trash": p["in_trash"],
                        "child_page": {"title": p["properties"]["title"]["title"][0]["text"]["content"]}}
                       for p in batch]
            more = start + 1 < len(pages)
            return {"results": results, "has_more": more, "next_cursor": str(start + 1) if more else None}
        note_id = parts[1]
        if note_id not in self.pages:
            raise NotFound("Fixture page not found.")
        page = self.pages[note_id]
        if method == "GET":
            if len(parts) == 3:
                assert parts[2] == "markdown"
                return {"markdown": self.bodies[note_id], "truncated": self.truncated, "unknown_block_ids": []}
            return deepcopy(page)
        assert method == "PATCH"
        if len(parts) == 3:
            assert payload["type"] == "replace_content"
            self.bodies[note_id] = self.normalize_markdown(payload["replace_content"]["new_str"])
        else:
            page["in_trash"] = payload["in_trash"]
        page["last_edited_time"] = self.revision()
        if len(parts) == 3:
            return {"object": "page_markdown", "id": note_id, "markdown": self.bodies[note_id],
                    "truncated": self.truncated, "unknown_block_ids": []}
        return deepcopy(page)

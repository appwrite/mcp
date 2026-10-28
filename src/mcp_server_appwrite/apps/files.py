"""``view=files``: a Storage bucket's files, with image previews.

The list is a page of a bucket's files, newest first, optionally narrowed by a
search over file names. With ``file_id`` the payload also carries that file in
full.

The view has no network access, so it cannot load a file's preview URL. With
``thumbnails`` the payload carries small previews of the page's images (and a
larger one of the opened file) as data URIs, rendered by Appwrite's image
transformations through the same hidden catalog read the model could make.
The model's own call leaves them out, since some hosts put
``structuredContent`` in the model's context; the view asks for them itself.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import Any

from appwrite_console.query import Query

from ..constants import SHOW_TOOL_NAME

Read = Callable[[str, Any, dict[str, Any]], dict[str, Any]]
"""Runs a hidden read tool: ``(tool name, project ID, arguments)``."""

Preview = Callable[[Any, dict[str, Any]], str | None]
"""Renders ``storage_get_file_preview`` for ``(project ID, arguments)`` as a
data URI, ``None`` when Appwrite cannot."""

PAGE_SIZE = 24
"""Files per page: fills the grid's rows of two, three, four and six."""

THUMBNAIL = 240
"""Thumbnail edge in pixels: twice the grid's cell, for dense screens."""

PREVIEW = 960
"""Width of the opened file's preview in pixels."""

PREVIEWABLE = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/avif",
        "image/heic",
        "image/bmp",
    }
)
"""Types Appwrite's image transformations accept."""

PARALLEL = 8
"""Previews rendered at once. Each is one Appwrite request of a few hundred
milliseconds, so a page of 24 takes about three rounds."""

SUMMARY_ROWS = 10


def read(read: Read, preview: Preview, arguments: dict[str, Any]) -> dict[str, Any]:
    project_id = arguments.get("project_id")
    bucket_id = arguments.get("bucket_id")
    if not isinstance(bucket_id, str) or not bucket_id:
        raise ValueError("bucket_id is required for view=files.")
    search = arguments.get("search") or None
    cursor = arguments.get("cursor") or None
    file_id = arguments.get("file_id") or None
    thumbnails = arguments.get("thumbnails") is True
    target = {"bucket_id": bucket_id}

    bucket = read("storage_get_bucket", project_id, target)
    # One past the page tells whether another page follows.
    queries = [Query.order_desc("$createdAt"), Query.limit(PAGE_SIZE + 1)]
    if cursor:
        queries.append(Query.cursor_after(str(cursor)))
    listing: dict[str, Any] = {**target, "queries": queries}
    if search:
        listing["search"] = str(search)
    page = read("storage_list_files", project_id, listing)
    rows = [_file(each) for each in page.get("files") or []]
    more = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    transforms = bool(bucket.get("transformations", True))

    opened = None
    if file_id:
        opened = _file(
            read("storage_get_file", project_id, {**target, "file_id": file_id})
        )

    if thumbnails and transforms:
        # (file, key, preview arguments) for every image preview to render.
        wanted = [
            (
                row,
                "thumbnail",
                {"width": THUMBNAIL, "height": THUMBNAIL, "quality": 70},
            )
            for row in rows
            if row["mimeType"] in PREVIEWABLE
        ]
        if opened and opened["mimeType"] in PREVIEWABLE:
            wanted.append((opened, "preview", {"width": PREVIEW, "quality": 80}))
        with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
            # Each render runs in a copy of this request's context, which holds
            # the caller's credentials.
            futures = [
                pool.submit(
                    copy_context().run,
                    preview,
                    project_id,
                    {**target, "file_id": each["id"], "output": "webp", **options},
                )
                for each, _, options in wanted
            ]
        for (each, key, _), future in zip(wanted, futures):
            each[key] = future.result()
    return {
        "view": "files",
        "projectId": project_id,
        "bucketId": bucket_id,
        "bucket": {
            "name": bucket.get("name") or bucket_id,
            "maximumFileSize": bucket.get("maximumFileSize"),
            "allowedFileExtensions": bucket.get("allowedFileExtensions") or [],
            "encryption": bool(bucket.get("encryption")),
            "antivirus": bool(bucket.get("antivirus")),
            "compression": bucket.get("compression") or "none",
            "fileSecurity": bool(bucket.get("fileSecurity")),
            "transformations": transforms,
        },
        "search": search,
        "files": rows,
        "total": int(page.get("total", len(rows))),
        "cursor": cursor,
        "next": rows[-1]["id"] if more else None,
        "file": opened,
        "thumbnails": thumbnails,
    }


def _file(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": document.get("$id"),
        "name": document.get("name") or "",
        "mimeType": document.get("mimeType") or "",
        "size": int(document.get("sizeOriginal") or 0),
        "createdAt": document.get("$createdAt"),
        "updatedAt": document.get("$updatedAt"),
        "folder": document.get("folder") or "",
        "encryption": bool(document.get("encryption")),
        "compression": document.get("compression") or "none",
        "chunksTotal": int(document.get("chunksTotal") or 0),
        "chunksUploaded": int(document.get("chunksUploaded") or 0),
        "permissions": [str(each) for each in document.get("$permissions") or []],
        "thumbnail": None,
        "preview": None,
    }


def size(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    amount = value / 1024
    for unit in ("KB", "MB"):
        if amount < 1024:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} GB"


def summary(data: dict[str, Any]) -> str:
    rows: list[dict[str, Any]] = data["files"]
    scope = f" matching {data['search']!r}" if data["search"] else ""
    lines = [
        f"Bucket {data['bucket']['name']} ({data['bucketId']}): {data['total']} "
        f"files{scope}, showing {len(rows)} newest"
        + (" after the cursor." if data["cursor"] else ".")
    ]
    if rows:
        lines.append("id | name | type | size | uploaded")
        for row in rows[:SUMMARY_ROWS]:
            lines.append(
                " | ".join(
                    [
                        row["id"],
                        row["name"],
                        row["mimeType"],
                        size(row["size"]),
                        row["createdAt"] or "",
                    ]
                )
            )
        if len(rows) > SUMMARY_ROWS:
            lines.append(f"… {len(rows) - SUMMARY_ROWS} more shown in the view.")
    if data["next"]:
        lines.append(
            f"Next page: {SHOW_TOOL_NAME} with view=files and cursor={data['next']}."
        )
    opened = data["file"]
    if opened:
        lines.append(
            f"File {opened['id']} ({opened['name']}): {opened['mimeType']}, "
            f"{size(opened['size'])}, uploaded {opened['createdAt']}"
            + (", encrypted" if opened["encryption"] else "")
            + "."
        )
    return "\n".join(lines)

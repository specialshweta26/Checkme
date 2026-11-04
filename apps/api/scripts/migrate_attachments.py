"""Copy attachments between the Render disk and object storage. Runbook: docs/ATTACHMENT_STORAGE.md.

    python scripts/migrate_attachments.py check             # write, read and delete one test object
    python scripts/migrate_attachments.py copy              # disk -> bucket, reports only
    python scripts/migrate_attachments.py copy --apply
    python scripts/migrate_attachments.py verify            # downloads every object and compares it with the disk
    python scripts/migrate_attachments.py restore           # bucket -> disk (rollback), reports only
    python scripts/migrate_attachments.py restore --apply

It uses STORAGE_DISK_PATH and the S3_* settings, and ignores FILE_STORAGE, so it works before and after the switch.
Every command is safe to run again: identical files are skipped, and a file that differs is reported and left alone
unless --replace-different is passed. Nothing is ever deleted except check's own test object. It exits non-zero if
anything failed, differs or is missing.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import os
import sys
import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.storage import guess_media_type  # noqa: E402

SIDECAR = ".ctype"  # app.storage keeps a file's content type next to it, in <name>.ctype
PROBE_PREFIX = "_migration-probe/"
CHUNK = 1024 * 1024


@dataclass
class DiskFile:
    key: str
    path: Path
    content_type: str


def iter_disk_files(root: Path) -> Iterator[DiskFile]:
    """Every attachment under root. A <name>.ctype file is metadata only when <name> sits next to it, so an uploaded
    file that happens to be called report.ctype is still copied."""
    for folder, dirs, names in os.walk(root):
        dirs.sort()
        present = set(names)
        for name in sorted(names):
            path = Path(folder) / name
            if path.is_symlink():
                continue
            if name.endswith(SIDECAR) and name[: -len(SIDECAR)] in present:
                continue
            sidecar = path.with_name(name + SIDECAR)
            stored = sidecar.read_text(encoding="utf-8").strip() if sidecar.exists() else ""
            yield DiskFile(path.relative_to(root).as_posix(), path, stored or guess_media_type(name))


def _digest(path: Path, algorithm: str = "md5") -> tuple[str, int]:
    h = hashlib.new(algorithm)
    size = 0
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _head(client, bucket: str, key: str) -> dict | None:
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise


def _matches(head: dict, md5: str, size: int) -> bool:
    return head.get("ContentLength") == size and (head.get("ETag") or "").strip('"') == md5


def _say_failure(counts: Counter, key: str, exc: Exception) -> None:
    counts["failed"] += 1
    print(f"FAILED {key}: {type(exc).__name__}: {exc}", file=sys.stderr)


def copy_to_bucket(client, bucket: str, root: Path, apply: bool = False, replace_different: bool = False) -> Counter:
    counts: Counter = Counter()
    for item in iter_disk_files(root):
        counts["disk_files"] += 1
        try:
            md5, size = _digest(item.path)
            head = _head(client, bucket, item.key)
            if head is not None and _matches(head, md5, size):
                counts["already_there"] += 1
                continue
            if head is not None and not replace_different:
                counts["different"] += 1
                print(f"DIFFERENT {item.key}: bucket has {head.get('ContentLength')} bytes, disk has {size}")
                continue
            if not apply:
                counts["would_copy"] += 1
                continue
            content_md5 = base64.b64encode(bytes.fromhex(md5)).decode()
            with item.path.open("rb") as fh:
                # ContentMD5 makes the store reject the upload if the bytes changed on the way.
                client.put_object(Bucket=bucket, Key=item.key, Body=fh, ContentType=item.content_type, ContentMD5=content_md5)
            counts["copied"] += 1
            counts["copied_bytes"] += size
        except (ClientError, OSError) as exc:
            _say_failure(counts, item.key, exc)
    return counts


def verify(client, bucket: str, root: Path) -> Counter:
    """Download every object and compare its SHA-256 with the disk. Does not trust sizes or ETags."""
    counts: Counter = Counter()
    for item in iter_disk_files(root):
        counts["disk_files"] += 1
        try:
            want, size = _digest(item.path, "sha256")
            try:
                obj = client.get_object(Bucket=bucket, Key=item.key)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                    counts["missing"] += 1
                    print(f"MISSING {item.key}")
                    continue
                raise
            h = hashlib.sha256()
            got_size = 0
            for chunk in obj["Body"].iter_chunks(CHUNK):
                h.update(chunk)
                got_size += len(chunk)
            if h.hexdigest() != want or got_size != size:
                counts["different"] += 1
                print(f"DIFFERENT {item.key}: bucket has {got_size} bytes, disk has {size}")
                continue
            counts["ok"] += 1
            counts["ok_bytes"] += size
            if obj.get("ContentType") != item.content_type:
                counts["type_differs"] += 1  # the bytes are safe; links also carry their own type
                print(f"TYPE {item.key}: bucket says {obj.get('ContentType')}, disk says {item.content_type}")
        except (ClientError, OSError) as exc:
            _say_failure(counts, item.key, exc)
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        counts["bucket_objects"] += sum(1 for o in page.get("Contents") or [] if not o["Key"].startswith(PROBE_PREFIX))
    return counts


def _write_from_bucket(client, bucket: str, key: str, dest: Path) -> int:
    """Write through a temp file, so a dropped connection never leaves half a file under the real name."""
    obj = client.get_object(Bucket=bucket, Key=key)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{uuid.uuid4().hex}.tmp")
    size = 0
    try:
        with tmp.open("wb") as fh:
            for chunk in obj["Body"].iter_chunks(CHUNK):
                fh.write(chunk)
                size += len(chunk)
        dest.with_name(dest.name + SIDECAR).write_text(obj.get("ContentType") or "", encoding="utf-8")
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return size


def restore_to_disk(client, bucket: str, root: Path, apply: bool = False, replace_different: bool = False) -> Counter:
    counts: Counter = Counter()
    base = root.resolve()
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for entry in page.get("Contents") or []:
            key = entry["Key"]
            if key.startswith(PROBE_PREFIX):
                continue
            counts["bucket_objects"] += 1
            try:
                dest = (base / key).resolve()
                if not dest.is_relative_to(base):
                    raise ValueError("key points outside the disk folder")
                if dest.exists():
                    md5, size = _digest(dest)
                    if entry.get("Size") == size and (entry.get("ETag") or "").strip('"') == md5:
                        counts["already_there"] += 1
                        continue
                    if not replace_different:
                        counts["different"] += 1
                        print(f"DIFFERENT {key}: bucket has {entry.get('Size')} bytes, disk has {size}")
                        continue
                if not apply:
                    counts["would_restore"] += 1
                    continue
                counts["restored_bytes"] += _write_from_bucket(client, bucket, key, dest)
                counts["restored"] += 1
            except (ClientError, OSError, ValueError) as exc:
                _say_failure(counts, key, exc)
    return counts


def check(client, bucket: str) -> None:
    """Prove these credentials can write, read and delete before anything depends on them."""
    key = f"{PROBE_PREFIX}{uuid.uuid4().hex}"
    body = b"checkme attachment storage check"
    content_md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
    try:
        # Both upload shapes in use: bytes (app.storage.put_bytes), then a file with ContentMD5 (copy_to_bucket).
        client.put_object(Bucket=bucket, Key=key, Body=body, ContentType="text/plain")
        client.put_object(Bucket=bucket, Key=key, Body=io.BytesIO(body), ContentType="text/plain", ContentMD5=content_md5)
        obj = client.get_object(Bucket=bucket, Key=key)
        if obj["Body"].read() != body or obj.get("ContentType") != "text/plain":
            raise RuntimeError("the test object came back changed")
    finally:
        client.delete_object(Bucket=bucket, Key=key)


def problems(counts: Counter) -> int:
    return counts["failed"] + counts["different"] + counts["missing"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Copy attachments between the disk and object storage.")
    parser.add_argument("command", choices=["check", "copy", "verify", "restore"])
    parser.add_argument("--apply", action="store_true", help="copy or restore for real; without it, only report")
    parser.add_argument("--replace-different", action="store_true", help="overwrite a destination file that differs")
    args = parser.parse_args(argv)

    from app.config import get_settings
    from app.storage import _client

    settings = get_settings()
    root = Path(settings.storage_disk_path)
    bucket = settings.s3_bucket
    print(f"disk={root} endpoint={settings.s3_endpoint_url} bucket={bucket}")
    client = _client()

    if args.command == "check":
        check(client, bucket)
        print("check ok: wrote, read and deleted a test object")
        return 0

    if args.command in {"copy", "verify"} and not next(iter_disk_files(root), None):
        # Never report success because the disk path was wrong or not mounted.
        print(f"No files found under {root}. Check STORAGE_DISK_PATH and that the disk is mounted.", file=sys.stderr)
        return 2

    if args.command == "copy":
        counts = copy_to_bucket(client, bucket, root, args.apply, args.replace_different)
    elif args.command == "verify":
        counts = verify(client, bucket, root)
    else:
        counts = restore_to_disk(client, bucket, root, args.apply, args.replace_different)
    print(" ".join(f"{name}={counts[name]}" for name in sorted(counts)) or "nothing to do")
    if args.command != "verify" and not args.apply:
        print("Report only. Nothing was written. Add --apply to run it.")
    return 1 if problems(counts) else 0


if __name__ == "__main__":
    raise SystemExit(main())

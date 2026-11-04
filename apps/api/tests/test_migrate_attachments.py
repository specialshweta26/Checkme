"""The attachment copy script, against an in-memory S3 stand-in and the real app.storage disk and S3 paths."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody

from app import storage
from app.config import get_settings

_spec = importlib.util.spec_from_file_location(
    "migrate_attachments", Path(__file__).resolve().parents[1] / "scripts" / "migrate_attachments.py"
)
mig = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mig  # dataclasses look their module up here
_spec.loader.exec_module(mig)

BUCKET = "attachments"


def _error(code: str, op: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


class FakeS3:
    def __init__(self, page_size: int = 2):
        self.objects: dict[str, dict] = {}
        self.puts: list[str] = []
        self.page_size = page_size
        self.fail_keys: set[str] = set()

    def put_object(self, Bucket, Key, Body, ContentType="", ContentMD5=None):
        if Key in self.fail_keys:
            raise _error("InternalError", "PutObject")
        data = Body if isinstance(Body, bytes) else Body.read()
        md5 = hashlib.md5(data).digest()
        if ContentMD5 and base64.b64decode(ContentMD5) != md5:
            raise _error("BadDigest", "PutObject")
        self.objects[Key] = {"data": data, "type": ContentType, "etag": f'"{md5.hex()}"'}
        self.puts.append(Key)

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _error("404", "HeadObject")
        row = self.objects[Key]
        return {"ContentLength": len(row["data"]), "ETag": row["etag"], "ContentType": row["type"]}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _error("NoSuchKey", "GetObject")
        row = self.objects[Key]
        return {"Body": StreamingBody(io.BytesIO(row["data"]), len(row["data"])), "ContentType": row["type"]}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self

    def paginate(self, Bucket):
        keys = sorted(self.objects)
        for start in range(0, max(len(keys), 1), self.page_size):
            page = keys[start : start + self.page_size]
            yield {
                "Contents": [
                    {"Key": k, "Size": len(self.objects[k]["data"]), "ETag": self.objects[k]["etag"]} for k in page
                ]
            }


@pytest.fixture
def s3():
    return FakeS3()


@pytest.fixture
def disk(tmp_path, monkeypatch):
    """Files written the way the app writes them, by app.storage in disk mode."""
    root = tmp_path / "attachments"
    monkeypatch.setattr(get_settings(), "file_storage", "disk")
    monkeypatch.setattr(get_settings(), "storage_disk_path", str(root))
    storage.put_bytes("u1/aaa/report.pdf", b"%PDF-1", "application/pdf")
    storage.put_bytes("u1/bbb/photo.png", b"\x89PNG", "image/png")
    storage.put_bytes("u2/ccc/notes.txt", "नमस्ते".encode(), "text/plain")
    return root


def test_sidecars_are_metadata_not_attachments(disk):
    assert sorted((f.key, f.content_type) for f in mig.iter_disk_files(disk)) == [
        ("u1/aaa/report.pdf", "application/pdf"),
        ("u1/bbb/photo.png", "image/png"),
        ("u2/ccc/notes.txt", "text/plain"),
    ]


def test_an_upload_named_like_a_sidecar_is_still_copied(disk):
    storage.put_bytes("u3/ddd/report.ctype", b"mine", "application/x-thing")
    assert {f.key: f.content_type for f in mig.iter_disk_files(disk)}["u3/ddd/report.ctype"] == "application/x-thing"


def test_a_file_without_a_sidecar_gets_a_guessed_type(disk):
    (disk / "u1/aaa/report.pdf.ctype").unlink()
    assert {f.key: f.content_type for f in mig.iter_disk_files(disk)}["u1/aaa/report.pdf"] == "application/pdf"


def test_report_only_writes_nothing(disk, s3):
    counts = mig.copy_to_bucket(s3, BUCKET, disk)
    assert counts["would_copy"] == 3
    assert s3.objects == {}


def test_after_the_switch_the_app_reads_every_copied_file(disk, s3, monkeypatch):
    before = {f.key: storage.get_bytes(f.key) for f in mig.iter_disk_files(disk)}
    assert mig.copy_to_bucket(s3, BUCKET, disk, apply=True)["copied"] == 3
    assert not any(k.endswith(".ctype") for k in s3.objects)

    monkeypatch.setattr(get_settings(), "file_storage", "s3")
    monkeypatch.setattr(storage, "_client", lambda: s3)
    assert {key: storage.get_bytes(key) for key in before} == before


def test_a_second_run_copies_only_new_files(disk, s3):
    mig.copy_to_bucket(s3, BUCKET, disk, apply=True)
    s3.puts.clear()
    storage.put_bytes("u1/eee/late.txt", b"arrived during the switch", "text/plain")
    counts = mig.copy_to_bucket(s3, BUCKET, disk, apply=True)
    assert s3.puts == ["u1/eee/late.txt"]
    assert counts["already_there"] == 3
    assert counts["copied"] == 1


def test_a_differing_object_is_reported_and_kept_until_replacement_is_asked_for(disk, s3):
    s3.put_object(BUCKET, "u1/aaa/report.pdf", b"partial", "application/pdf")
    counts = mig.copy_to_bucket(s3, BUCKET, disk, apply=True)
    assert counts["different"] == 1
    assert s3.objects["u1/aaa/report.pdf"]["data"] == b"partial"
    assert mig.problems(counts) == 1

    counts = mig.copy_to_bucket(s3, BUCKET, disk, apply=True, replace_different=True)
    assert s3.objects["u1/aaa/report.pdf"]["data"] == b"%PDF-1"
    assert mig.problems(counts) == 0


def test_one_failed_upload_does_not_stop_the_rest_and_is_counted(disk, s3):
    s3.fail_keys.add("u1/aaa/report.pdf")
    counts = mig.copy_to_bucket(s3, BUCKET, disk, apply=True)
    assert counts["failed"] == 1
    assert counts["copied"] == 2
    s3.fail_keys.clear()
    assert mig.copy_to_bucket(s3, BUCKET, disk, apply=True)["copied"] == 1


def test_verify_passes_only_when_every_byte_matches(disk, s3):
    mig.copy_to_bucket(s3, BUCKET, disk, apply=True)
    counts = mig.verify(s3, BUCKET, disk)
    assert counts["ok"] == 3
    assert counts["bucket_objects"] == 3
    assert mig.problems(counts) == 0

    # Same size and a matching ETag: only reading the bytes can catch this.
    s3.objects["u1/aaa/report.pdf"]["data"] = b"%PDF-2"
    del s3.objects["u1/bbb/photo.png"]
    counts = mig.verify(s3, BUCKET, disk)
    assert counts["different"] == 1
    assert counts["missing"] == 1
    assert counts["ok"] == 1


def test_verify_reports_a_lost_content_type_without_failing(disk, s3):
    mig.copy_to_bucket(s3, BUCKET, disk, apply=True)
    s3.objects["u1/bbb/photo.png"]["type"] = "binary/octet-stream"
    counts = mig.verify(s3, BUCKET, disk)
    assert counts["type_differs"] == 1
    assert mig.problems(counts) == 0


def test_rollback_restores_new_uploads_in_the_format_disk_mode_reads(disk, s3):
    mig.copy_to_bucket(s3, BUCKET, disk, apply=True)
    s3.put_object(BUCKET, "u1/fff/after-switch.pdf", b"%PDF-new", "application/pdf")

    assert mig.restore_to_disk(s3, BUCKET, disk)["would_restore"] == 1
    assert not (disk / "u1/fff/after-switch.pdf").exists()

    counts = mig.restore_to_disk(s3, BUCKET, disk, apply=True)
    assert counts["restored"] == 1
    assert counts["already_there"] == 3
    assert storage.get_bytes("u1/fff/after-switch.pdf") == (b"%PDF-new", "application/pdf")
    assert not list(disk.rglob("*.tmp"))


def test_restore_rebuilds_an_empty_disk(disk, s3, tmp_path, monkeypatch):
    mig.copy_to_bucket(s3, BUCKET, disk, apply=True)
    fresh = tmp_path / "new-disk"
    monkeypatch.setattr(get_settings(), "storage_disk_path", str(fresh))
    assert mig.restore_to_disk(s3, BUCKET, fresh, apply=True)["restored"] == 3
    assert storage.get_bytes("u1/bbb/photo.png") == (b"\x89PNG", "image/png")


def test_restore_keeps_a_differing_disk_file(disk, s3):
    s3.put_object(BUCKET, "u1/aaa/report.pdf", b"other", "application/pdf")
    counts = mig.restore_to_disk(s3, BUCKET, disk, apply=True)
    assert counts["different"] == 1
    assert (disk / "u1/aaa/report.pdf").read_bytes() == b"%PDF-1"


def test_restore_refuses_a_key_that_leaves_the_disk_folder(disk, s3, tmp_path):
    s3.put_object(BUCKET, "../escaped.txt", b"x", "text/plain")
    counts = mig.restore_to_disk(s3, BUCKET, disk, apply=True)
    assert counts["failed"] == 1
    assert not (tmp_path / "escaped.txt").exists()


def test_a_dropped_download_leaves_no_partial_file(disk, s3, monkeypatch):
    s3.put_object(BUCKET, "u9/ggg/big.bin", b"data", "application/octet-stream")

    class Broken:
        def iter_chunks(self, _size):
            yield b"da"
            raise OSError("connection dropped")

    monkeypatch.setattr(s3, "get_object", lambda Bucket, Key: {"Body": Broken(), "ContentType": "x"})
    counts = mig.restore_to_disk(s3, BUCKET, disk, apply=True)
    assert counts["failed"] == 1
    assert not (disk / "u9/ggg").exists() or not list((disk / "u9/ggg").iterdir())


def test_check_leaves_nothing_behind(s3):
    mig.check(s3, BUCKET)
    assert s3.objects == {}
    assert s3.puts and s3.puts[0].startswith(mig.PROBE_PREFIX)


def test_probe_objects_are_ignored_by_restore_and_verify(disk, s3):
    s3.put_object(BUCKET, f"{mig.PROBE_PREFIX}left-over", b"x", "text/plain")
    mig.copy_to_bucket(s3, BUCKET, disk, apply=True)
    assert mig.verify(s3, BUCKET, disk)["bucket_objects"] == 3
    assert mig.restore_to_disk(s3, BUCKET, disk, apply=True)["bucket_objects"] == 3


def test_an_empty_or_unmounted_disk_is_an_error_not_a_success(tmp_path, monkeypatch, s3, capsys):
    monkeypatch.setattr(get_settings(), "storage_disk_path", str(tmp_path / "not-mounted"))
    monkeypatch.setattr(storage, "_client", lambda: s3)
    assert mig.main(["verify"]) == 2
    assert mig.main(["copy", "--apply"]) == 2
    assert "No files found" in capsys.readouterr().err


def test_main_returns_non_zero_when_something_is_missing(disk, s3, monkeypatch):
    monkeypatch.setattr(storage, "_client", lambda: s3)
    assert mig.main(["verify"]) == 1
    assert mig.main(["copy", "--apply"]) == 0
    assert mig.main(["verify"]) == 0

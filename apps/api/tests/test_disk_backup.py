"""The one-time disk backup copies files to R2 under the same key and never overwrites what is already there."""

from __future__ import annotations

from app.ops import disk_backup


class FakeS3:
    def __init__(self, existing=()):
        self.keys = {k: b"old" for k in existing}

    def head_object(self, Bucket, Key):
        if Key not in self.keys:
            raise KeyError(Key)

    def upload_fileobj(self, f, bucket, key, ExtraArgs=None):
        self.keys[key] = f.read()


def test_copies_missing_files_and_keeps_existing_ones(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "attachments" / "u1" / "a").mkdir(parents=True)
    (root / "attachments" / "u1" / "a" / "notes.txt").write_bytes(b"new notes")
    (root / "attachments" / "u1" / "a" / "notes.txt.ctype").write_text("text/plain")
    (root / "attachments" / "u2").mkdir(parents=True)
    (root / "attachments" / "u2" / "x.pdf").write_bytes(b"%PDF")
    (root / "other").mkdir()
    (root / "other" / "log.txt").write_bytes(b"log")
    s3 = FakeS3(existing={"u2/x.pdf"})
    monkeypatch.setattr("app.storage._client", lambda: s3)
    out = disk_backup.backup(root, root / "attachments")
    assert (out["files"], out["copied"], out["already"], out["failed"]) == (3, 2, 1, 0)
    assert s3.keys["u1/a/notes.txt"] == b"new notes"  # the key the app used when files lived on disk
    assert s3.keys["u2/x.pdf"] == b"old"  # already in R2: not overwritten
    assert s3.keys["disk-backup/other/log.txt"] == b"log"
    assert out["newest"] and set(out["top"]) == {"u1", "u2", "disk-backup"}
    assert disk_backup.backup(tmp_path / "nowhere")["missing"] is True

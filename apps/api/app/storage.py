from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.config import get_settings


def _disk_root() -> Path:
    root = Path(get_settings().storage_disk_path)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _disk_file(key: str) -> Path:
    dest = (_disk_root() / key).resolve()
    if not str(dest).startswith(str(_disk_root().resolve())):
        raise ValueError("invalid storage key")
    return dest


def guess_media_type(filename: str, content_type: str | None = None) -> str:
    name = (filename or "").lower()
    ctype = (content_type or "").split(";")[0].strip().lower()
    if name.endswith(".pdf") or ctype == "application/pdf":
        return "application/pdf"
    if name.endswith(".png") or ctype == "image/png":
        return "image/png"
    if name.endswith((".jpg", ".jpeg")) or ctype in {"image/jpeg", "image/jpg"}:
        return "image/jpeg"
    if name.endswith(".gif"):
        return "image/gif"
    if name.endswith(".webp"):
        return "image/webp"
    if name.endswith(".txt"):
        return "text/plain"
    if name.endswith(".html"):
        return "text/html"
    return ctype or "application/octet-stream"


def render_pdf_preview(data: bytes, max_pages: int = 2) -> bytes:
    import pypdfium2 as pdfium
    from PIL import Image

    pdf = pdfium.PdfDocument(data)
    images = []
    try:
        for i in range(min(len(pdf), max_pages)):
            page = pdf[i]
            try:
                images.append(page.render(scale=1.7).to_pil().convert("RGB"))
            finally:
                page.close()
    finally:
        pdf.close()
    if not images:
        raise ValueError("empty pdf")
    if len(images) == 1:
        out = images[0]
    else:
        width = max(im.width for im in images)
        gap = 16
        height = sum(im.height for im in images) + gap * (len(images) - 1)
        out = Image.new("RGB", (width, height), (255, 255, 255))
        y = 0
        for im in images:
            out.paste(im, (0, y))
            y += im.height + gap
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


@lru_cache
def _client():
    s = get_settings()
    endpoint = s.s3_endpoint_url
    if endpoint and "://" not in endpoint:
        endpoint = f"http://{endpoint}"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=s.s3_access_key,
        aws_secret_access_key=s.s3_secret_key,
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )


def ensure_bucket() -> None:
    if get_settings().use_disk_storage:
        _disk_root()
        return
    s = get_settings()
    client = _client()
    try:
        client.head_bucket(Bucket=s.s3_bucket)
    except ClientError:
        client.create_bucket(Bucket=s.s3_bucket)


def put_bytes(key: str, data: bytes, content_type: str) -> None:
    if get_settings().use_disk_storage:
        dest = _disk_file(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        dest.with_name(dest.name + ".ctype").write_text(content_type or "", encoding="utf-8")
        return
    _client().put_object(Bucket=get_settings().s3_bucket, Key=key, Body=data, ContentType=content_type)


def delete_keys(keys: list[str]) -> None:
    """Best-effort delete (old run screenshots)."""
    keys = [k for k in keys if k]
    if not keys:
        return
    if get_settings().use_disk_storage:
        for key in keys:
            dest = _disk_file(key)
            dest.unlink(missing_ok=True)
            dest.with_name(dest.name + ".ctype").unlink(missing_ok=True)
        return
    for start in range(0, len(keys), 1000):
        _client().delete_objects(Bucket=get_settings().s3_bucket, Delete={"Objects": [{"Key": k} for k in keys[start : start + 1000]], "Quiet": True})


def get_bytes(key: str) -> tuple[bytes, str]:
    if get_settings().use_disk_storage:
        dest = _disk_file(key)
        data = dest.read_bytes()
        meta = dest.with_name(dest.name + ".ctype")
        stored = meta.read_text(encoding="utf-8").strip() if meta.exists() else ""
        return data, stored or guess_media_type(dest.name)
    obj = _client().get_object(Bucket=get_settings().s3_bucket, Key=key)
    data = obj["Body"].read()
    return data, obj.get("ContentType") or "application/octet-stream"


def file_url(key: str, filename: str, content_type: str, inline: bool = False) -> str:
    from urllib.parse import quote

    from app.security import sign_file_token

    token = sign_file_token({"key": key, "filename": filename, "content_type": content_type, "inline": inline})
    return f"{get_settings().public_web_url.rstrip('/')}/api/files?t={quote(token, safe='')}"


def signed_url(key: str, filename: str, expires: int = 0, inline: bool = False, content_type: str = "application/octet-stream") -> str:
    return file_url(key, filename, content_type, inline=inline)


def extract_text(filename: str, content_type: str, data: bytes, limit: int = 4000) -> str:
    name = filename.lower()
    if name.endswith(".txt") or name.endswith(".csv") or name.endswith(".md") or content_type.startswith("text/"):
        return data.decode("utf-8", "replace")[:limit]
    if name.endswith(".html") or content_type == "text/html":
        from connector_core.text import html_to_text

        return html_to_text(data.decode("utf-8", "replace"))[:limit]
    if name.endswith(".pdf") or content_type == "application/pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = []
        for page in reader.pages[:8]:
            pages.append(page.extract_text() or "")
        return "\n".join(pages)[:limit]
    if name.endswith(".docx"):
        from docx import Document

        doc = Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs)[:limit]
    if name.endswith(".pptx"):
        return _pptx_text(data)[:limit]
    if name.endswith((".xlsx", ".xlsm")):
        return _xlsx_text(data, limit)
    return ""


MAX_SHEET_ROWS = 500


def _xlsx_text(data: bytes, limit: int) -> str:
    """Each sheet's rows as tab-separated text, from the XML inside the file. Formulas show their last saved value."""
    import re
    import zipfile
    from xml.etree import ElementTree

    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

    def number(name: str) -> int:
        found = re.search(r"(\d+)\.xml$", name)
        return int(found.group(1)) if found else 0

    with zipfile.ZipFile(io.BytesIO(data)) as book:
        names = book.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            for item in ElementTree.fromstring(book.read("xl/sharedStrings.xml")).iter(f"{ns}si"):
                shared.append("".join(t.text or "" for t in item.iter(f"{ns}t")))
        titles = []
        if "xl/workbook.xml" in names:
            titles = [s.get("name") or "" for s in ElementTree.fromstring(book.read("xl/workbook.xml")).iter(f"{ns}sheet")]
        out: list[str] = []
        sheets = sorted((n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)), key=number)
        for index, sheet in enumerate(sheets):
            title = titles[index] if index < len(titles) else f"Sheet {number(sheet)}"
            out.append(f"Sheet: {title}")
            for count, row in enumerate(ElementTree.fromstring(book.read(sheet)).iter(f"{ns}row")):
                if count >= MAX_SHEET_ROWS:
                    out.append(f"(more rows not shown)")
                    break
                cells = []
                for cell in row.iter(f"{ns}c"):
                    kind = cell.get("t") or ""
                    value = cell.find(f"{ns}v")
                    text = value.text if value is not None and value.text is not None else ""
                    if kind == "s" and text.isdigit() and int(text) < len(shared):
                        text = shared[int(text)]
                    elif kind == "inlineStr":
                        text = "".join(t.text or "" for t in cell.iter(f"{ns}t"))
                    cells.append(text)
                if any(cells):
                    out.append("\t".join(cells))
            if sum(len(line) for line in out) > limit:
                break
    return "\n".join(out)[:limit]


def _pptx_text(data: bytes) -> str:
    """Slide text, in slide order, from the XML inside the file."""
    import re
    import zipfile
    from xml.etree import ElementTree

    def number(name: str) -> int:
        found = re.search(r"(\d+)\.xml$", name)
        return int(found.group(1)) if found else 0

    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as pptx:
        slides = [n for n in pptx.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
        for name in sorted(slides, key=number):
            root = ElementTree.fromstring(pptx.read(name))
            lines = [node.text for node in root.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}t") if node.text]
            out.append(f"Slide {number(name)}: " + " ".join(lines))
    return "\n".join(out)


def list_keys(prefix: str) -> list[str]:
    """Every stored file key under ``prefix`` (a user's files live under "<user id>/")."""
    if not prefix or "/" not in prefix:
        raise ValueError("a prefix like '<user id>/' is required")
    if get_settings().use_disk_storage:
        root = _disk_file(prefix)
        if not root.exists():
            return []
        base = _disk_file("")
        return [str(p.relative_to(base)) for p in root.rglob("*") if p.is_file() and not p.name.endswith(".ctype")]
    keys: list[str] = []
    token = None
    while True:
        kwargs = {"Bucket": get_settings().s3_bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = _client().list_objects_v2(**kwargs)
        keys.extend(obj["Key"] for obj in page.get("Contents") or [])
        if not page.get("IsTruncated"):
            return keys
        token = page.get("NextContinuationToken")

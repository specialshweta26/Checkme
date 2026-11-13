"""Designed PowerPoint decks for the website and the desktop app.

The chat agent writes a pptxgenjs script. This runs it on the API, in a temp directory, with no
secrets and no network, then attaches the .pptx. Both surfaces use this tool, so the user's
machine does not need Node or PowerPoint.
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

MAX_SCRIPT = 80_000
MAX_BYTES = 25_000_000
TIMEOUT = 45

_BANNED = re.compile(
    r"\brequire\s*\(|\bimport\s*\(|process\s*\.\s*(?:env|binding|mainModule)|globalThis|Function\s*\(|\beval\s*\("
    r"|child_process|\bfetch\s*\(|WebSocket|XMLHttpRequest"
)

_RUNNER = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");
const net = require("net");
const http = require("http");
const https = require("https");

function blocked() {
  const err = new Error("network is disabled");
  err.code = "ENETUNREACH";
  throw err;
}
net.Socket.prototype.connect = blocked;
net.createConnection = blocked;
net.connect = blocked;
http.request = blocked;
http.get = blocked;
https.request = blocked;
https.get = blocked;
if (typeof globalThis.fetch === "function") globalThis.fetch = async () => blocked();
try {
  const undici = require("undici");
  undici.request = blocked;
  undici.fetch = blocked;
} catch (err) { /* undici is not a direct dependency */ }

const loaded = require("pptxgenjs");
const PptxGenJS = loaded.default || loaded;
const userCode = fs.readFileSync(process.argv[2], "utf8");
const OUT = process.argv[3];
const made = [];

function pptxgen(...args) {
  const deck = new PptxGenJS(...args);
  made.push(deck);
  const write = deck.writeFile.bind(deck);
  deck.writeFile = (opts) => write(Object.assign({}, opts || {}, { fileName: OUT }));
  return deck;
}
Object.assign(pptxgen, PptxGenJS);

const sandbox = {
  pptxgen,
  PptxGenJS: pptxgen,
  OUT,
  console: {
    log() {},
    warn() {},
    error(...parts) { process.stderr.write(parts.map(String).join(" ") + "\n"); },
  },
};
const script = new vm.Script("(async () => {\n" + userCode + "\n})()", { filename: "deck.js" });
Promise.resolve(script.runInContext(vm.createContext(sandbox), { timeout: 20000 }))
  .then(async () => {
    if (!fs.existsSync(OUT)) {
      const deck = made[made.length - 1];
      if (!deck) throw new Error("script never created a presentation (new pptxgen())");
      await deck.writeFile({ fileName: OUT });
    }
    const size = fs.existsSync(OUT) ? fs.statSync(OUT).size : 0;
    if (size < 1000) throw new Error("presentation file was not written");
    process.exit(0);
  })
  .catch((err) => {
    process.stderr.write((err && err.stack) ? err.stack : String(err));
    process.stderr.write("\n");
    process.exit(1);
  });
"""


class PresentationError(ValueError):
    pass


def node_binary() -> str:
    override = os.environ.get("PPTX_NODE", "").strip()
    if override and os.path.isfile(override):
        return override
    return shutil.which("node") or ""


def modules_dir() -> Path:
    override = os.environ.get("PPTX_NODE_MODULES", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "pptx" / "node_modules"


def _reject_script(script: str) -> None:
    text = script or ""
    if not text.strip():
        raise PresentationError("script is empty")
    if len(text) > MAX_SCRIPT:
        raise PresentationError(f"script is longer than {MAX_SCRIPT} characters")
    if _BANNED.search(text):
        raise PresentationError(
            "script may only build a deck with the provided pptxgen constructor. "
            "Remove require, import, process, fetch, eval, and Function."
        )


def _permission_args(readable: list[Path], writable: Path) -> list[str]:
    args = ["--permission"]
    for path in readable:
        args.append(f"--allow-fs-read={path}")
    args.append(f"--allow-fs-write={writable}")
    return args


def render(script: str) -> bytes:
    """Run a pptxgenjs script and return the .pptx bytes."""
    _reject_script(script)
    node = node_binary()
    modules = modules_dir()
    if not node:
        raise PresentationError("Node is not installed, so the deck could not be built.")
    if not (modules / "pptxgenjs").is_dir():
        raise PresentationError("pptxgenjs is not installed next to the API (apps/api/pptx).")
    work = Path(tempfile.mkdtemp(prefix="checkme-pptx-"))
    try:
        work = work.resolve()
        user_path = work / "deck.js"
        runner_path = work / "run.js"
        out_path = work / "deck.pptx"
        user_path.write_text(script, encoding="utf-8")
        runner_path.write_text(_RUNNER, encoding="utf-8")
        node_bin = Path(node).resolve().parent
        node_home = node_bin.parent
        readable = [modules.resolve(), work, node_bin, node_home / "lib"]
        command = [node, *_permission_args(readable, work), str(runner_path), str(user_path), str(out_path)]
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "NODE_PATH": str(modules.resolve()),
            "HOME": str(work),
            "TMPDIR": str(work),
        }
        try:
            done = subprocess.run(
                command,
                cwd=str(work),
                env=env,
                capture_output=True,
                timeout=TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PresentationError("The deck script took too long.") from exc
        if done.returncode != 0 or not out_path.is_file():
            detail = (done.stderr or done.stdout or b"").decode("utf-8", "replace").strip()
            raise PresentationError(f"Couldn't build the deck: {detail[:400] or 'no file written'}")
        data = out_path.read_bytes()
        if len(data) > MAX_BYTES:
            raise PresentationError("The deck is too large.")
        if not zipfile.is_zipfile(io.BytesIO(data)):
            raise PresentationError("The script did not write a PowerPoint file.")
        return data
    finally:
        shutil.rmtree(work, ignore_errors=True)


def review(data: bytes) -> dict:
    """Slide count, and which slides are still title-and-text with no visual."""
    from pptx import Presentation
    from pptx.enum.dml import MSO_FILL
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    try:
        deck = Presentation(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise PresentationError(f"Couldn't read the deck back: {type(exc).__name__}") from exc
    plain: list[int] = []
    for index, slide in enumerate(deck.slides, 1):
        if not _slide_has_visual(slide, MSO_SHAPE_TYPE, MSO_FILL):
            plain.append(index)
    if len(deck.slides) == 0:
        raise PresentationError("The deck has no slides.")
    return {"slides": len(deck.slides), "plain_slides": plain}


def _slide_has_visual(slide: Any, shape_type: Any, fill: Any) -> bool:
    """A text box is a rectangle with no fill. A card, shape, chart, or image is a visual."""
    from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE

    drawn = {
        shape_type.CHART,
        shape_type.PICTURE,
        shape_type.FREEFORM,
        shape_type.GROUP,
        shape_type.LINE,
        shape_type.TABLE,
    }
    for shape in slide.shapes:
        if shape.shape_type in drawn:
            return True
        if shape.shape_type != shape_type.AUTO_SHAPE:
            continue
        if _filled(shape, fill):
            return True
        try:
            if shape.auto_shape_type != MSO_AUTO_SHAPE_TYPE.RECTANGLE:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _filled(shape: Any, fill: Any) -> bool:
    try:
        kind = shape.fill.type
    except Exception:  # noqa: BLE001
        return False
    return kind not in (None, fill.BACKGROUND)


async def create(ctx: Any, filename: str, script: str) -> dict:
    from app.agent.attachments import store_blob
    from app.agent.office_files import file_name
    from connector_core import BinaryResult

    try:
        data = await asyncio.to_thread(render, script or "")
        info = review(data)
    except PresentationError as exc:
        return {"ok": False, "error": str(exc)[:500]}
    name = file_name(filename, "pptx")
    card = await store_blob(
        ctx.user.id,
        BinaryResult(
            filename=name,
            content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            data=data,
        ),
    )
    ctx.attachments.append(card)
    result = {
        "ok": True,
        "filename": name,
        "size": len(data),
        "slides": info["slides"],
        "download_url": card.get("download_url"),
    }
    if info["plain_slides"]:
        numbers = ", ".join(str(n) for n in info["plain_slides"])
        result["plain_slides"] = info["plain_slides"]
        result["warning"] = (
            f"Slides {numbers} are text only. Rebuild the deck with a shape, card, chart, or image on those slides "
            "before you tell the user it is done."
        )
    return result

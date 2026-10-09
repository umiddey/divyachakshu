"""Bounded extraction of real content from supported conversation documents.

Supported inputs are read as actual content, not described:

* UTF-8 text/Markdown/CSV/JSON/log/YAML files
* PDF documents (text layer only; no OCR, no rendering, no remote resources)
* DOCX documents (stdlib ZIP + XML, with member and decompression limits)

Everything runs in a fixed, non-model-controlled helper subprocess with a hard
wall timeout and a bounded output contract. Nothing is executed: no macros, no
scripts, no recursive archive expansion, no external entity resolution, and no
network access. Files whose actual bytes do not match their claimed type are
refused rather than parsed on trust.
"""

from __future__ import annotations

import asyncio
import json
import sys
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from .records import (
    MAX_EXTRACTED_DOC_BYTES,
    MAX_EXTRACTED_DOC_CHARS,
    STATUS_FAILED,
    STATUS_TEXT,
    STATUS_TOO_LARGE,
)

PARSER_TIMEOUT_SECONDS = 20.0
MAX_DOC_BYTES = 16 * 1024 * 1024
# DOCX hardening: a fixed member budget, a total uncompressed budget, and a
# per-member budget. A zip bomb fails on these limits before XML parsing.
MAX_DOCX_MEMBERS = 2_000
MAX_DOCX_TOTAL_UNCOMPRESSED = 64 * 1024 * 1024
MAX_DOCX_MEMBER_UNCOMPRESSED = 8 * 1024 * 1024
MAX_DOCX_RATIO = 200
MAX_PDF_PAGES = 500
MAX_XML_DEPTH_CHARS = 4 * 1024 * 1024

PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"


@dataclass(frozen=True)
class DocumentResult:
    """Extraction outcome for one document payload."""

    status: str
    text: str = ""
    truncated: bool = False
    limitation: str = ""


def sniff_kind(filename: str, payload: bytes) -> str:
    """Classify a payload from its ACTUAL bytes, falling back to the name.

    A misleading extension or declared media type cannot promote a payload
    into a format the bytes do not support.

    Args:
        filename: Attachment filename (advisory).
        payload: The real bytes.

    Returns:
        One of ``"pdf"``, ``"docx"``, ``"text"``, or ``"unsupported"``.
    """
    if payload.startswith(PDF_MAGIC):
        return "pdf"
    if payload[:4] == ZIP_MAGIC:
        # DOCX is an OPC package: it must contain the word/document part.
        if _looks_like_docx(payload):
            return "docx"
        return "unsupported"
    lowered = filename.lower()
    if lowered.endswith((".pdf",)):
        return "unsupported"  # claimed PDF without a PDF header
    if lowered.endswith((".docx", ".doc", ".xls", ".xlsx", ".pptx", ".zip")):
        return "unsupported"
    return "text"


def _looks_like_docx(payload: bytes) -> bool:
    """Return True when the ZIP package holds the DOCX main document part."""
    try:
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError, ValueError):
        return False
    return "word/document.xml" in names


async def extract_document(filename: str, payload: bytes) -> DocumentResult:
    """Extract bounded text from one supported document payload.

    Text formats are decoded in-process. PDF and DOCX are parsed in a fixed
    helper subprocess with a hard timeout so a pathological document cannot
    stall the event loop or exhaust the daemon.

    Args:
        filename: Attachment filename, used only for text decoding.
        payload: The real document bytes.

    Returns:
        A :class:`DocumentResult`. Unsupported or oversized payloads return a
        non-text status with an explicit limitation instead of a fabricated
        summary.
    """
    if len(payload) > MAX_DOC_BYTES:
        return DocumentResult(status=STATUS_TOO_LARGE, limitation="document exceeds the size limit")
    kind = sniff_kind(filename, payload)
    if kind == "text":
        return _extract_text(payload)
    if kind == "pdf":
        return await _extract_via_helper("pdf", payload)
    if kind == "docx":
        return await _extract_via_helper("docx", payload)
    return DocumentResult(
        status=STATUS_FAILED,
        limitation="unsupported file type; contents were not read",
    )


def _extract_text(payload: bytes) -> DocumentResult:
    """Decode a strict UTF-8 text document.

    Undecodable bytes mean the payload is not a text document: replacing them
    would fabricate content the sender never wrote, so the payload is refused
    with an honest limitation instead.
    """
    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError:
        return DocumentResult(
            status=STATUS_FAILED,
            limitation="content is not valid UTF-8 text",
        )
    if "\x00" in decoded:
        return DocumentResult(
            status=STATUS_FAILED,
            limitation="binary content is not valid UTF-8 text",
        )
    return _clip(decoded)


def _clip(text: str) -> DocumentResult:
    """Apply the extracted-character budget and report clipping."""
    if len(text) <= MAX_EXTRACTED_DOC_CHARS:
        return DocumentResult(status=STATUS_TEXT, text=text)
    clipped = text[:MAX_EXTRACTED_DOC_CHARS].rstrip()
    return DocumentResult(
        status=STATUS_TEXT,
        text=clipped,
        truncated=True,
        limitation=f"clipped to {MAX_EXTRACTED_DOC_CHARS} characters",
    )


async def _extract_via_helper(kind: str, payload: bytes) -> DocumentResult:
    """Run the fixed parser helper under a hard wall timeout."""
    request = json.dumps({"kind": kind, "payload_b64": _b64(payload)})
    # The helper runs as a fixed script path under isolated mode: no shell,
    # no model-controlled argument, no user site-packages, no PYTHONPATH.
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(helper_script_path()),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": "0"},
        )
    except OSError:
        return DocumentResult(status=STATUS_FAILED, limitation="document parser unavailable")
    assert process.stdin is not None and process.stdout is not None
    try:
        stdout, _ = await asyncio.wait_for(
            process.communicate(request.encode("ascii")), timeout=PARSER_TIMEOUT_SECONDS
        )
    except asyncio.CancelledError:
        await _terminate(process)
        raise
    except asyncio.TimeoutError:
        await _terminate(process)
        return DocumentResult(status=STATUS_FAILED, limitation="document parsing timed out")
    except OSError:
        await _terminate(process)
        return DocumentResult(status=STATUS_FAILED, limitation="document could not be parsed")
    if process.returncode != 0:
        return DocumentResult(status=STATUS_FAILED, limitation="document could not be parsed")
    return _decode_helper_result(stdout)


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """Kill a helper worker and await its exit so none is ever left running."""
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except (asyncio.TimeoutError, ProcessLookupError, OSError):
        pass


def _decode_helper_result(stdout: bytes) -> DocumentResult:
    """Decode the helper's bounded JSON contract, failing closed."""
    if len(stdout) > MAX_EXTRACTED_DOC_BYTES * 2:
        return DocumentResult(status=STATUS_FAILED, limitation="document parser output exceeded bounds")
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return DocumentResult(status=STATUS_FAILED, limitation="document parser returned no usable result")
    if not isinstance(payload, dict):
        return DocumentResult(status=STATUS_FAILED, limitation="document parser returned no usable result")
    status = payload.get("status")
    if status == STATUS_TEXT:
        text = payload.get("text")
        if not isinstance(text, str):
            return DocumentResult(status=STATUS_FAILED, limitation="document parser returned no text")
        return _clip(text)
    limitation = payload.get("limitation")
    return DocumentResult(
        status=STATUS_FAILED,
        limitation=limitation if isinstance(limitation, str) and limitation
        else "no text could be extracted from this document",
    )


def _b64(payload: bytes) -> str:
    """Base64-encode a payload for the helper's stdin contract."""
    import base64

    return base64.b64encode(payload).decode("ascii")


def helper_script_path() -> Path:
    """Return the fixed helper module path (diagnostics only)."""
    return Path(__file__).with_name("_docparse.py")

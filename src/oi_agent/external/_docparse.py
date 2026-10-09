"""Fixed, non-model-controlled document extraction helper.

Invoked as ``python -I <this file>`` by :mod:`oi_agent.external.documents`.
It reads exactly one JSON request from stdin and writes exactly one JSON
result to stdout, using the SHARED status vocabulary in ``_status.py`` so the
parent and this helper can never disagree about what a status means.

Hardening that matters for untrusted input:

* Resource limits are applied to this process before any parsing: CPU seconds,
  address space, file size, and core size. A malicious document therefore
  cannot exhaust the daemon even if it defeats the wall timeout.
* The request budget is derived from the accepted document size, so a document
  the parent already agreed to accept is never rejected here for being large.
* PDF text extraction only. No rendering, no OCR, no JavaScript, no embedded
  file following, no network access, and no page images.
* DOCX is read with the stdlib ZIP reader under member-count, total-size,
  per-member-size, and compression-ratio budgets, so a zip bomb is refused
  before any XML is parsed.
* XML is parsed only after an explicit DTD/entity scan rejects the document.
  The stdlib ``XMLParser`` has no documented entity-resolution switch, so
  claiming one would be false; without a DOCTYPE there is no internal subset
  and therefore no entity to declare or expand.
* Clipping is reported in the result, so the parent never claims full text
  when the helper returned a prefix.
* Every failure returns a bounded status plus a short, fixed limitation
  phrase. Never a document body, a stack trace, or a filesystem path.
"""

from __future__ import annotations

import base64
import io
import json
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import defusedxml.ElementTree as SafeET
from defusedxml import DefusedXmlException

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _status import STATUS_TEXT  # noqa: E402

MAX_CHARS = 24_000
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_PDF_PAGES = 500
MAX_PDF_PAGE_CHARS = 20_000
MAX_DOCX_MEMBERS = 2_000
MAX_DOCX_TOTAL_UNCOMPRESSED = 64 * 1024 * 1024
MAX_DOCX_MEMBER_UNCOMPRESSED = 8 * 1024 * 1024
MAX_DOCX_RATIO = 200
MAX_XML_BYTES = 4 * 1024 * 1024

# Accepted source bytes, mirrored from documents.MAX_DOC_BYTES. The base64
# request is ~4/3 of that plus the JSON envelope, and stdin is read with a
# small margin above it so an accepted document is never refused here.
MAX_DOC_BYTES = 16 * 1024 * 1024
MAX_REQUEST_BYTES = (MAX_DOC_BYTES * 4 // 3) + (1024 * 1024)

CPU_SECONDS = 15
ADDRESS_SPACE_BYTES = 1_500 * 1024 * 1024

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _apply_resource_limits() -> None:
    """Bound this worker's CPU, memory, and file size.

    Best effort: a platform without ``resource`` still relies on the parent's
    wall timeout, but on Linux these make a decompression bomb fail fast
    instead of consuming the host.
    """
    try:
        import resource
    except ImportError:
        return
    for limit, value in (
        (resource.RLIMIT_CPU, CPU_SECONDS),
        (resource.RLIMIT_AS, ADDRESS_SPACE_BYTES),
        (resource.RLIMIT_FSIZE, MAX_OUTPUT_BYTES),
        (resource.RLIMIT_CORE, 0),
    ):
        try:
            soft, hard = resource.getrlimit(limit)
            ceiling = value if hard == resource.RLIM_INFINITY else min(value, hard)
            resource.setrlimit(limit, (ceiling, hard))
        except (ValueError, OSError, resource.error):
            continue


def _emit(status: str, text: str = "", limitation: str = "", truncated: bool = False) -> None:
    """Write one bounded JSON result and exit."""
    result: dict[str, object] = {"status": status}
    if text:
        result["text"] = text[:MAX_CHARS]
        result["truncated"] = truncated
    if limitation:
        result["limitation"] = limitation
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    sys.stdout.flush()
    raise SystemExit(0)


def _clip(text: str) -> tuple[str, bool]:
    """Normalize whitespace and clip, reporting whether clipping happened."""
    collapsed = " ".join(text.split())
    if len(collapsed) > MAX_CHARS:
        return collapsed[:MAX_CHARS], True
    return collapsed, False


def extract_pdf(payload: bytes) -> None:
    """Extract the text layer of a bounded PDF page range."""
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError:
        _emit("failed", limitation="PDF support is not installed")
        return
    try:
        reader = PdfReader(io.BytesIO(payload), strict=False)
        if reader.is_encrypted:
            _emit("failed", limitation="the PDF is encrypted; no text was extractable")
            return
        pages = reader.pages
        if len(pages) > MAX_PDF_PAGES:
            _emit("failed", limitation=f"the PDF exceeds the {MAX_PDF_PAGES}-page limit")
            return
        chunks: list[str] = []
        for index, page in enumerate(pages):
            if index >= MAX_PDF_PAGES:
                break
            try:
                extracted = page.extract_text() or ""
            except (PdfReadError, ValueError, KeyError, RecursionError, MemoryError):
                continue
            if extracted:
                chunks.append(extracted[:MAX_PDF_PAGE_CHARS])
    except (PdfReadError, ValueError, KeyError, RecursionError, MemoryError, OSError):
        _emit("failed", limitation="the PDF could not be read")
        return
    text, truncated = _clip("\n".join(chunks))
    if not text.strip():
        _emit(
            "failed",
            limitation="no text was extractable from this PDF (it may be scanned)",
        )
    _emit(STATUS_TEXT, text=text, truncated=truncated)


def _docx_within_limits(archive: zipfile.ZipFile) -> bool:
    """Return True when the package respects every zip-bomb budget."""
    infos = archive.infolist()
    if len(infos) > MAX_DOCX_MEMBERS:
        return False
    total = 0
    for info in infos:
        if info.file_size > MAX_DOCX_MEMBER_UNCOMPRESSED:
            return False
        if info.compress_size > 0 and info.file_size / info.compress_size > MAX_DOCX_RATIO:
            return False
        total += info.file_size
        if total > MAX_DOCX_TOTAL_UNCOMPRESSED:
            return False
    return True


def _docx_text(root: ET.Element) -> str:
    """Return each paragraph once.

    Walking only ``w:p`` elements and reading their descendant ``w:t`` nodes
    yields each run exactly once. Collecting paragraphs AND separately
    collecting every ``w:t`` would append each string twice.
    """
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{W_NS}p"):
        runs = "".join(node.text or "" for node in paragraph.iter(f"{W_NS}t"))
        if runs.strip():
            paragraphs.append(runs)
    if paragraphs:
        return " ".join(paragraphs)
    # Some producers omit the namespace; fall back to the unprefixed name.
    paragraphs = []
    for paragraph in root.iter("p"):
        runs = "".join(node.text or "" for node in paragraph.iter("t"))
        if runs.strip():
            paragraphs.append(runs)
    return " ".join(paragraphs)


def extract_docx(payload: bytes) -> None:
    """Extract paragraph text from a bounded DOCX package."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except (zipfile.BadZipFile, OSError, ValueError):
        _emit("failed", limitation="the DOCX package could not be opened")
        return
    with archive:
        if not _docx_within_limits(archive):
            _emit("failed", limitation="the DOCX package exceeds the decompression limits")
            return
        try:
            info = archive.getinfo("word/document.xml")
        except KeyError:
            _emit("failed", limitation="the DOCX has no readable document body")
            return
        if info.file_size > MAX_XML_BYTES:
            _emit("failed", limitation="the DOCX document part exceeds the size limit")
            return
        try:
            raw = archive.read("word/document.xml")
        except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):
            _emit("failed", limitation="the DOCX document part could not be read")
            return
    if len(raw) > MAX_XML_BYTES:
        _emit("failed", limitation="the DOCX document part exceeds the size limit")
        return
    # defusedxml refuses DTDs, entity expansion, and external references
    # regardless of the declared encoding. A byte-regex prefilter would be
    # encoding-dependent (UTF-16 bypassed the previous ASCII scan), and the
    # stdlib parser cannot disable entity resolution.
    try:
        root = SafeET.fromstring(raw)
    except DefusedXmlException:
        _emit("failed", limitation="the DOCX document body declares a DTD or entity")
        return
    except ET.ParseError:
        _emit("failed", limitation="the DOCX document body is not well-formed XML")
        return
    text, truncated = _clip(_docx_text(root))
    if not text.strip():
        _emit("failed", limitation="the DOCX contains no extractable text")
    _emit(STATUS_TEXT, text=text, truncated=truncated)


def main() -> int:
    """Read one bounded request and emit one bounded result."""
    _apply_resource_limits()
    try:
        request = json.loads(sys.stdin.read(MAX_REQUEST_BYTES))
    except (ValueError, OSError, UnicodeDecodeError):
        _emit("failed", limitation="the parser request was not valid JSON")
        return 0
    if not isinstance(request, dict):
        _emit("failed", limitation="the parser request was not an object")
        return 0
    encoded = request.get("payload_b64")
    if not isinstance(encoded, str):
        _emit("failed", limitation="the parser request carried no payload")
        return 0
    if len(encoded) > MAX_REQUEST_BYTES:
        _emit("failed", limitation="the parser request exceeded the input budget")
        return 0
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        _emit("failed", limitation="the parser payload was not valid base64")
        return 0
    kind = request.get("kind")
    if kind == "pdf":
        extract_pdf(payload)
    elif kind == "docx":
        extract_docx(payload)
    else:
        _emit("failed", limitation="unsupported document kind")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

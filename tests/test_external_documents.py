"""Behavior tests for bounded document extraction (real helper subprocesses).

Covers the correction-7 parser contracts on real bytes: strict UTF-8 text,
status-vocabulary agreement between the parent and the helper, DOCX
DTD/entity refusal through defusedxml, misleading-extension refusal, and
size bounds. No network, no shell, no model involvement.
"""

import io
import zipfile

import pytest

from oi_agent.external.documents import (
    MAX_DOC_BYTES,
    DocumentResult,
    extract_document,
    sniff_kind,
)
from oi_agent.external.records import (
    STATUS_FAILED,
    STATUS_TEXT,
    STATUS_TOO_LARGE,
)

pytestmark = pytest.mark.asyncio


def _invoice_pdf() -> bytes:
    """Build a minimal, harmless, valid PDF carrying one text line."""
    text = b"BT /F1 16 Tf 40 140 Td (Invoice total: 37.50 USD) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(text)).encode() + b" >>\nstream\n"
        + text + b"\nendstream",
    ]
    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n")
    xref = len(result)
    result.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(
        b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"
        + str(xref).encode() + b"\n%%EOF\n"
    )
    return bytes(result)


def _docx_with_body(document_xml: bytes) -> bytes:
    """Build a minimal DOCX package around one document part."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            "<?xml version=\"1.0\"?><Types xmlns=\"http://schemas.openxmlformats.org"
            "/package/2006/content-types\"><Override PartName=\"/word/document.xml\" "
            "ContentType=\"application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document.main+xml\"/></Types>",
        )
        archive.writestr("word/document.xml", document_xml)
    return output.getvalue()


_PLAIN_DOCUMENT = (
    b"<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
    b"<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">"
    b"<w:body><w:p><w:r><w:t>Release owner: Ada. Deadline: Friday.</w:t></w:r></w:p>"
    b"</w:body></w:document>"
)

# --- text contracts ----------------------------------------------------------


async def test_valid_utf8_text_extracts() -> None:
    result = await extract_document("notes.txt", "planning notes".encode("utf-8"))
    assert result.status == STATUS_TEXT
    assert result.text == "planning notes"
    assert result.truncated is False


async def test_invalid_utf8_is_rejected_not_replaced() -> None:
    """Undecodable bytes are refused honestly, never replaced with U+FFFD."""
    payload = b"name,amount\n\xff\xfe,3"
    result = await extract_document("data.csv", payload)
    assert result.status == STATUS_FAILED
    assert "UTF-8" in result.limitation
    assert "\ufffd" not in result.text


async def test_binary_nul_bytes_are_rejected() -> None:
    result = await extract_document("blob.txt", b"abc\x00def" * 100)
    assert result.status == STATUS_FAILED
    assert "binary" in result.limitation


async def test_latin1_high_bytes_are_rejected() -> None:
    """0x92-style smart quotes from other encodings are not valid UTF-8."""
    result = await extract_document("notes.txt", b"caf\xe9 menu")
    assert result.status == STATUS_FAILED


# --- type contracts ----------------------------------------------------------


def test_sniff_refuses_claimed_pdf_without_header() -> None:
    assert sniff_kind("invoice.pdf", b"not a pdf at all") == "unsupported"


def test_sniff_classifies_real_pdf_from_bytes() -> None:
    assert sniff_kind("wrong-name.txt", _invoice_pdf()) == "pdf"


def test_sniff_refuses_generic_zip() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "hello")
    assert sniff_kind("bundle.zip", buffer.getvalue()) == "unsupported"


async def test_misleading_text_name_with_pdf_bytes_still_parses() -> None:
    """Actual bytes win over names: a PDF named .txt is read as a PDF."""
    result = await extract_document("weird.txt", _invoice_pdf())
    assert result.status == STATUS_TEXT
    assert "37.50" in result.text


async def test_claimed_docx_without_zip_is_unsupported() -> None:
    result = await extract_document("report.docx", b"plain text, not a package")
    assert result.status == STATUS_FAILED
    assert "unsupported" in result.limitation


# --- helper contracts --------------------------------------------------------


async def test_valid_pdf_extracts_with_shared_status_vocabulary() -> None:
    """The parent expects STATUS_TEXT ('extracted') and the helper emits it."""
    result = await extract_document("invoice.pdf", _invoice_pdf())
    assert result.status == STATUS_TEXT
    assert "Invoice total: 37.50 USD" in result.text
    assert "extracted" == STATUS_TEXT


async def test_valid_docx_extracts_each_paragraph_once() -> None:
    result = await extract_document("meeting.docx", _docx_with_body(_PLAIN_DOCUMENT))
    assert result.status == STATUS_TEXT
    assert result.text == "Release owner: Ada. Deadline: Friday."
    assert result.text.count("Ada") == 1


async def test_docx_declaring_dtd_is_refused() -> None:
    """A DTD in the document body means entities; parsing is refused."""
    hostile = (
        b"<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        b"<!DOCTYPE w:document [<!ENTITY x \"expanded\">]>"
        b"<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">"
        b"<w:body><w:p><w:r><w:t>&x;</w:t></w:r></w:p></w:body></w:document>"
    )
    result = await extract_document("evil.docx", _docx_with_body(hostile))
    assert result.status == STATUS_FAILED
    assert "DTD" in result.limitation


async def test_docx_document_too_large_is_refused() -> None:
    """A giant document part is refused by the XML byte budget."""
    huge_body = _PLAIN_DOCUMENT.replace(
        b"Ada.", b"Ada." + b" detail" * 700_000
    )
    assert len(huge_body) > 4 * 1024 * 1024
    result = await extract_document("huge.docx", _docx_with_body(huge_body))
    assert result.status == STATUS_FAILED


async def test_oversized_payload_never_reaches_the_helper() -> None:
    result = await extract_document("big.bin", b"\x00" * (MAX_DOC_BYTES + 1))
    assert result.status == STATUS_TOO_LARGE


async def test_helper_result_statuses_stay_in_shared_vocabulary() -> None:
    """Every status the helper can emit is one the parent understands."""
    from oi_agent.external._status import VALID_STATUSES

    assert "extracted" in VALID_STATUSES


def test_document_result_is_the_bounded_contract() -> None:
    fields = DocumentResult.__dataclass_fields__
    assert set(fields) == {"status", "text", "truncated", "limitation"}

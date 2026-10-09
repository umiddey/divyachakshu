"""Platform-neutral, bounded records for external conversation inputs.

The controller (not the model) prepares every byte that leaves the trust
boundary. This module defines the immutable metadata records the rest of OI
passes between the chat adapter, the retrieval layer, and the native runner,
plus the aggregate byte/count caps that bound a single run.

Privacy contract (repository invariant): raw attachment bytes and retrieved
article text live ONLY in the in-memory :class:`PreparedInputs` bundle or in
job-private temp files that are deleted when the run ends. They never reach OI
SQLite, logs, reflection retries, or behavioral memory. The records here carry
metadata only.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .._tempowner import mark_owner

# --- Aggregate caps for one run -------------------------------------------------
# Conservative defaults: a chat reply must stay small enough for both the
# native prompt bound and Discord. Operators widen them with `oi config set`.
MAX_ATTACHMENTS = 4
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 16 * 1024 * 1024
MAX_EXTRACTED_DOC_CHARS = 24_000
MAX_EXTRACTED_DOC_BYTES = 512 * 1024
MAX_IMAGE_PIXELS = 12_000_000
MAX_IMAGE_FRAMES = 20
MAX_IMAGE_DIMENSION = 8_000
MAX_EVIDENCE_ITEMS = 8
MAX_EVIDENCE_CHARS = 12_000
MAX_URL_BYTES = 2 * 1024 * 1024

# Bounded status vocabulary. Anything else is a bug and must fail closed.
STATUS_TEXT = "extracted"
STATUS_IMAGE = "image"
STATUS_UNSUPPORTED = "unsupported"
STATUS_TOO_LARGE = "too_large"
STATUS_FAILED = "failed"
STATUS_LIMIT = "limit_reached"

IMAGE_MEDIA_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")

DOCUMENT_TEXT_SUFFIXES = (".txt", ".md", ".markdown", ".csv", ".json", ".log", ".yaml", ".yml")
DOCUMENT_PDF_SUFFIXES = (".pdf",)
DOCUMENT_DOCX_SUFFIXES = (".docx",)


@dataclass(frozen=True)
class AttachmentInput:
    """Bounded metadata for one user attachment.

    Carries no raw bytes: the payload stays in :class:`PreparedInputs`. Every
    field is metadata a consumer can safely log (filename, declared type,
    declared size, status, and an explicit limitation list).
    """

    attachment_id: str
    source_message_id: int
    filename: str
    declared_media_type: str
    declared_size: int
    kind: str
    status: str
    limitations: tuple[str, ...] = ()

    @property
    def is_usable(self) -> bool:
        """Return True when the attachment can reach the model as content."""
        return self.status in (STATUS_TEXT, STATUS_IMAGE)

    def describe(self) -> str:
        """Render one bounded, log-safe metadata line."""
        notes = f" ({'; '.join(self.limitations)})" if self.limitations else ""
        return (
            f"{self.filename} [{self.kind}/{self.status}] "
            f"declared={self.declared_media_type or 'unknown'} "
            f"bytes={self.declared_size}{notes}"
        )


@dataclass(frozen=True)
class EvidenceItem:
    """One bounded piece of public external evidence.

    ``text`` is a controller-bounded excerpt of a public source, not a stored
    body: it exists only inside the in-memory bundle for the current run.
    """

    kind: str
    title: str
    url: str
    source_name: str
    retrieved_at: str
    published_at: str | None = None
    text: str = ""
    truncated: bool = False
    limitations: tuple[str, ...] = ()

    def describe(self) -> str:
        """Render citation metadata without the evidence body."""
        stamp = self.published_at or "publication time unavailable"
        notes = f" [{'; '.join(self.limitations)}]" if self.limitations else ""
        return f"{self.title} — {self.source_name} — {stamp} — {self.url}{notes}"


@dataclass
class PreparedInputs:
    """In-memory payload bundle for one run, deleted deterministically.

    The controller fills this before invoking the native runner. The runner
    reads from it; nothing else does, and it is never serialized to OI
    storage.
    """

    attachments: list[AttachmentInput] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)
    _blobs: dict[str, bytes] = field(default_factory=dict, repr=False)
    _files: dict[str, Path] = field(default_factory=dict, repr=False)
    _workdir: Path | None = field(default=None, repr=False)
    notes: list[str] = field(default_factory=list)

    @property
    def has_external_content(self) -> bool:
        """Return True when any attachment or evidence must be supplied."""
        return bool(self.attachments) or bool(self.evidence)

    def store_bytes(self, attachment_id: str, data: bytes) -> None:
        """Keep attachment bytes in memory only, bounded by the caller."""
        self._blobs[attachment_id] = data

    def store_file(self, name: str, data: bytes, suffix: str) -> Path:
        """Materialize bytes into the job-private workdir and return the path.

        Used for native image attachments, which must exist as real files at
        invocation time. The directory is removed by :meth:`close`.
        """
        path = self.workdir / f"{name}{suffix}"
        path.write_bytes(data)
        os_chmod_private(path)
        self._files[name] = path
        return path

    @property
    def workdir(self) -> Path:
        """Return the lazily created private temp directory for this run."""
        if self._workdir is None:
            self._workdir = Path(tempfile.mkdtemp(prefix="oi-external-"))
            _chmod_private_dir(self._workdir)
            mark_owner(self._workdir)
        return self._workdir

    def close(self) -> None:
        """Delete every transient byte this run created. Never raises."""
        self._blobs.clear()
        for path in self._files.values():
            try:
                path.unlink()
            except OSError:
                pass
        self._files.clear()
        if self._workdir is not None:
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None

    def __enter__(self) -> PreparedInputs:
        """Enter a managed scope so cleanup runs even on failure."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Delete transient bytes when leaving the scope."""
        self.close()


def _chmod_private_dir(path: Path) -> None:
    """Restrict one directory to the owner, ignoring unsupported platforms."""
    try:
        path.chmod(0o700)
    except OSError:
        pass


def os_chmod_private(path: Path) -> None:
    """Restrict one file to the owner, ignoring unsupported platforms."""
    try:
        path.chmod(0o600)
    except OSError:
        pass


def image_media_type_for(filename: str, head: bytes) -> str | None:
    """Detect a supported image type from ACTUAL bytes, not the name.

    The declared Discord media type and the file extension are advisory only;
    a misleading extension or MIME header must never change the decision.

    Args:
        filename: Attachment filename (advisory only).
        head: Leading bytes of the payload.

    Returns:
        The detected image media type, or ``None`` when unsupported.
    """
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def bounded_text(text: str, limit: int) -> tuple[str, bool]:
    """Clip text to ``limit`` characters on a clean boundary.

    Args:
        text: Untrusted text extracted by the controller.
        limit: Maximum characters to keep.

    Returns:
        Tuple of the clipped text and whether clipping occurred.
    """
    if len(text) <= limit:
        return text, False
    window = text[:limit]
    for boundary in ("\n\n", "\n", ". "):
        index = window.rfind(boundary)
        if index > limit // 2:
            return window[:index].rstrip() + " [clipped]", True
    return window.rstrip() + " [clipped]", True

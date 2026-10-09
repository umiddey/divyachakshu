"""The single shared extraction-status vocabulary.

Both sides of the document helper boundary must agree on these exact strings:
:mod:`oi_agent.external.documents` decides what a status MEANS, and
``oi_agent.external._docparse`` reports what it did. They previously disagreed
("extracted" vs "text"), which silently discarded every successful extraction.

This module has no imports on purpose: the helper runs under ``python -I`` as
a script and loads it by explicit path, so it stays dependency-free while
still sharing one definition with the parent process.
"""

from __future__ import annotations

STATUS_TEXT = "extracted"
STATUS_IMAGE = "image"
STATUS_UNSUPPORTED = "unsupported"
STATUS_TOO_LARGE = "too_large"
STATUS_FAILED = "failed"
STATUS_LIMIT = "limit_reached"

# Every status the helper may emit. Anything else fails closed.
VALID_STATUSES = frozenset({
    STATUS_TEXT, STATUS_UNSUPPORTED, STATUS_TOO_LARGE, STATUS_FAILED, STATUS_LIMIT,
})

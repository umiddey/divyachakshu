"""Controller-owned, bounded handling of external conversation inputs.

Everything that crosses into the trust boundary from outside the repository
lives here: public web/news retrieval, attached documents, and attached
images. The controller prepares and bounds every byte; the model only ever
receives already-validated content and can neither request retrieval itself
nor widen any permission.
"""

from __future__ import annotations

from .intent import PublicRefusal, PublicRequest, extract_public_request
from .records import (
    AttachmentInput,
    EvidenceItem,
    PreparedInputs,
)
from .retrieval import FetchedDocument, RetrievalError, fetch_public_document, search_news

__all__ = [
    "AttachmentInput",
    "EvidenceItem",
    "FetchedDocument",
    "PreparedInputs",
    "PublicRefusal",
    "PublicRequest",
    "RetrievalError",
    "extract_public_request",
    "fetch_public_document",
    "search_news",
]

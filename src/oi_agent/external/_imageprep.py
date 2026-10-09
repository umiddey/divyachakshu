"""Fixed, non-model-controlled image validation helper.

Invoked as ``python -I <this file>`` by :mod:`oi_agent.external.images`. It
speaks one bounded JSON contract on stdin/stdout:

``{"media_type": ..., "payload_b64": ...}`` -> ``{"ok", "width", "height",
"frames"}`` or ``{"ok": false, "limitation": ...}``.

The payload is decoded with Pillow's decompression-bomb guard, its real format
is confirmed from the decoded content (so a misleading extension or declared
media type cannot promote a payload), and its dimensions and frame count are
measured. Nothing here executes, installs, or fetches anything, and no
filesystem path, payload, or library traceback ever reaches stdout.
"""

from __future__ import annotations

import base64
import io
import json
import sys

MAX_DECLARED_PIXELS = 64_000_000
MAX_REQUEST_BYTES = 32 * 1024 * 1024

# Only the four media types the native v2 attachment contract passes to a
# vision-capable model. Anything else is refused here, not later.
EXPECTED_FORMAT = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/gif": "GIF",
    "image/webp": "WEBP",
}


def _fail(limitation: str) -> None:
    """Emit one bounded failure and exit."""
    sys.stdout.write(json.dumps({"ok": False, "limitation": limitation}))
    sys.stdout.flush()
    raise SystemExit(0)


def _payload(request: dict) -> bytes:
    """Decode the base64 payload from a bounded request."""
    encoded = request.get("payload_b64")
    if not isinstance(encoded, str):
        _fail("the image request carried no payload")
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        _fail("the image payload was not valid base64")
        return b""


def main() -> int:
    """Read one bounded request and emit one bounded validation result."""
    try:
        request = json.loads(sys.stdin.read(MAX_REQUEST_BYTES))
    except (ValueError, OSError, UnicodeDecodeError):
        _fail("the image helper request was not valid JSON")
        return 0
    if not isinstance(request, dict):
        _fail("the image helper request was not an object")
        return 0

    media_type = request.get("media_type")
    expected = EXPECTED_FORMAT.get(media_type) if isinstance(media_type, str) else None
    if expected is None:
        _fail("unsupported image media type")

    from PIL import Image

    Image.MAX_IMAGE_PIXELS = MAX_DECLARED_PIXELS
    raw = _payload(request)
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception:  # noqa: BLE001 - untrusted decoder boundary
        _fail("the image could not be decoded")
        return 0
    if (image.format or "").upper() != expected:
        _fail("the image bytes do not match the claimed image type")
        return 0

    width, height = image.size
    frames = int(getattr(image, "n_frames", 1) or 1)
    sys.stdout.write(json.dumps({
        "ok": True, "width": int(width), "height": int(height), "frames": frames,
    }))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

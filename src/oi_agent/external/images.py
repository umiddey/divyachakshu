"""Bounded validation and preparation of real image attachments.

Supported formats are PNG, JPEG, GIF, and WebP — the four types the native
v2 attachment contract passes to a vision-capable model. The decision is made
from the ACTUAL decoded bytes, so a misleading extension or declared media
type can never promote an unsupported or malformed payload.

Hardening:

* Animated GIF frame count and per-frame pixels are capped; a payload past
  the pixel/frame budget is refused, never silently attached.
* Total pixel budget and per-dimension caps are enforced before any scaling.
* Validation runs in a fixed, non-model-controlled helper subprocess with
  a hard wall timeout: a decompression bomb cannot stall or exhaust the daemon.
* A validated payload is attached as-is (still or animated within the caps);
  whether animation plays is the native viewer's business, never a reason to
  rewrite bytes.
* Every validated image becomes its own real file and its own native
  attachment. Repeated ``--file`` flags were verified against the installed
  native v2.0.5 to persist one attachment per flag (the prompt's user message
  carried a ``files`` list with every image), so multiple images reach the
  model at full fidelity instead of being composited into one sheet. Nothing is
  silently discarded: any overflow past the run's attachment cap is reported as
  an explicit limitation.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .records import (
    MAX_IMAGE_DIMENSION,
    MAX_IMAGE_FRAMES,
    MAX_IMAGE_PIXELS,
    STATUS_IMAGE,
    image_media_type_for,
)

VALIDATOR_TIMEOUT_SECONDS = 15.0
MAX_NATIVE_IMAGES = 8

PREPARED_SUFFIX = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


@dataclass(frozen=True)
class ImageInfo:
    """Validated metadata for one image payload."""

    media_type: str
    width: int
    height: int
    frames: int
    limitation: str = ""


@dataclass(frozen=True)
class PreparedImage:
    """One image ready to be attached natively, with its origin label."""

    label: str
    media_type: str
    path: Path
    limitation: str = ""


@dataclass
class ImagePreparation:
    """Result of preparing a set of images for one native invocation."""

    images: list[PreparedImage] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    @property
    def paths(self) -> list[Path]:
        """Return the file paths to attach natively, in order."""
        return [image.path for image in self.images]

    @property
    def labels(self) -> list[str]:
        """Return the bounded origin labels, in attachment order."""
        return [image.label for image in self.images]


async def inspect_image(payload: bytes) -> ImageInfo | None:
    """Validate one image payload in the fixed validator subprocess.

    Args:
        payload: The real image bytes.

    Returns:
        Validated metadata, or ``None`` when the payload is not a supported
        image or violates a pixel/frame budget.
    """
    media_type = image_media_type_for("", payload[:32])
    if media_type is None:
        return None
    stdout = await _run_validator({"media_type": media_type, "payload_b64": _b64(payload)})
    if stdout is None:
        return None
    try:
        result = json.loads(stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    width = result.get("width")
    height = result.get("height")
    frames = result.get("frames")
    if not all(isinstance(v, int) and v > 0 for v in (width, height, frames)):
        return None
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        return None
    if width * height > MAX_IMAGE_PIXELS:
        return None
    if frames < 1 or frames > MAX_IMAGE_FRAMES:
        return None
    return ImageInfo(
        media_type=media_type,
        width=width,
        height=height,
        frames=frames,
        limitation=(
            "animated image; all frames stay within the attachment caps"
            if frames > 1 else ""
        ),
    )


async def prepare_images(
    labeled_payloads: list[tuple[str, bytes, ImageInfo]],
    workdir: Path,
) -> ImagePreparation:
    """Write every validated image into ``workdir`` as a native attachment.

    Args:
        labeled_payloads: ``(label, bytes, validated info)`` triples in order.
        workdir: Job-private directory that the caller deletes afterwards.

    Returns:
        The prepared images and any explicit limitations. Each image keeps its
        own origin label so the prompt can tell the model which attachment is
        which, and overflow past the native cap is reported rather than hidden.
    """
    preparation = ImagePreparation()
    if not labeled_payloads:
        return preparation
    attached = labeled_payloads[:MAX_NATIVE_IMAGES]
    overflow = len(labeled_payloads) - len(attached)
    if overflow > 0:
        preparation.limitations.append(
            f"{overflow} additional image(s) exceeded the {MAX_NATIVE_IMAGES}-image "
            f"attachment limit and were not shown"
        )
    for index, (label, payload, info) in enumerate(attached, start=1):
        path = _write_image(workdir, f"image-{index}", payload, info.media_type)
        preparation.images.append(PreparedImage(
            label=label, media_type=info.media_type, path=path,
            limitation=info.limitation,
        ))
        if info.limitation:
            preparation.limitations.append(f"{label}: {info.limitation}")
    return preparation


def _write_image(workdir: Path, name: str, payload: bytes, media_type: str) -> Path:
    """Write one image payload into the private work directory."""
    path = workdir / f"{name}{PREPARED_SUFFIX.get(media_type, '.png')}"
    path.write_bytes(payload)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


async def _run_validator(request: dict) -> bytes | None:
    """Run the fixed image validator under a hard wall timeout."""
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(_validator_path()),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": "0"},
        )
    except OSError:
        return None
    assert process.stdin is not None and process.stdout is not None
    try:
        stdout, _ = await asyncio.wait_for(
            process.communicate(json.dumps(request).encode("utf-8")),
            timeout=VALIDATOR_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        await _terminate(process)
        raise
    except asyncio.TimeoutError:
        await _terminate(process)
        return None
    except OSError:
        await _terminate(process)
        return None
    if process.returncode != 0:
        return None
    return stdout


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """Kill a validator worker and await its exit so none is left running."""
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except (asyncio.TimeoutError, ProcessLookupError, OSError):
        pass


def _validator_path() -> Path:
    """Return the fixed image helper module path."""
    return Path(__file__).with_name("_imageprep.py")


def _b64(payload: bytes) -> str:
    """Base64-encode a payload for the helper's stdin contract."""
    import base64

    return base64.b64encode(payload).decode("ascii")


# Re-exported so callers can assert the native media contract in one place.
SUPPORTED_IMAGE_TYPES = tuple(PREPARED_SUFFIX)
IMAGE_STATUS = STATUS_IMAGE

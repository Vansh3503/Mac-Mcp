"""Building MCP tool results.

Every tool returns a :class:`mcp_types.CallToolResult`:

* success: compact text for the model (``content[0]``), optional images, and the complete
  data in ``structured_content`` (validated against the tool's ``outputSchema``);
* failure: ``is_error=True``, the error as one short text block (``CODE: message`` plus a
  ``Hint:`` line) and the machine-readable error under ``_meta["axpilot/error"]``.

Tools build successes with :func:`ok_result`; the guard turns every :class:`AXPilotError`
into :func:`error_result`, so tool bodies simply raise.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from mcp_types import CallToolResult, ContentBlock, ImageContent, TextContent
from pydantic import BaseModel

from axpilot.errors import AXPilotError
from axpilot.models import Capture

__all__ = [
    "ERROR_META_KEY",
    "Image",
    "error_code_of",
    "error_result",
    "image_content",
    "ok_result",
    "text_of",
]

ERROR_META_KEY = "axpilot/error"
"""``_meta`` key under which failed results carry :meth:`AXPilotError.to_dict`."""

_MIME_TYPES: dict[str, str] = {"png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg"}


@dataclass(frozen=True, slots=True)
class Image:
    """Encoded image bytes plus their MIME type (for images that are not a :class:`Capture`)."""

    data: bytes
    mime_type: str


def mime_type_for(fmt: str) -> str:
    """``"image/png"`` / ``"image/jpeg"`` for a capture format."""
    return _MIME_TYPES.get(fmt.lower(), f"image/{fmt.lower()}")


def image_content(image: Capture | Image) -> ImageContent:
    """An MCP image block (base64 data) for a capture or raw image."""
    if isinstance(image, Capture):
        data, mime = image.data, mime_type_for(image.format)
    else:
        data, mime = image.data, image.mime_type
    return ImageContent(type="image", data=base64.b64encode(data).decode("ascii"), mime_type=mime)


def _structured(structured: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(structured, BaseModel):
        return structured.model_dump(mode="json", by_alias=True)
    return dict(structured)


def ok_result(
    text: str,
    structured: BaseModel | Mapping[str, Any],
    *,
    images: Sequence[Capture | Image] = (),
    warnings: Sequence[str] = (),
) -> CallToolResult:
    """A successful result.

    ``structured`` (a pydantic output model, or a dict shaped like one) becomes
    ``structured_content``. ``warnings`` are appended to the text as ``warning: …`` lines and
    merged into ``structured_content["warnings"]`` (every output model has that field), so
    degraded results are visible to the model and to scripts.
    """
    data = _structured(structured)
    merged = [*data.get("warnings", []), *(w for w in warnings if w not in data.get("warnings", []))]
    if merged or "warnings" in data:
        data["warnings"] = merged
    lines = [text.rstrip()] if text.strip() else []
    lines.extend(f"warning: {w}" for w in merged)
    content: list[ContentBlock] = [TextContent(type="text", text="\n".join(lines) or "ok")]
    content.extend(image_content(image) for image in images)
    return CallToolResult(content=content, structured_content=data)


def error_result(err: AXPilotError) -> CallToolResult:
    """A failed result: ``is_error=True``, ``CODE: message`` (+ hint) as text, details in ``_meta``."""
    result = CallToolResult(content=[TextContent(type="text", text=err.to_text())], is_error=True)
    result.meta = {ERROR_META_KEY: err.to_dict()}
    return result


def error_code_of(result: CallToolResult) -> str | None:
    """The AXPilot error code of a failed result (``None`` for successes and foreign errors)."""
    if not result.is_error:
        return None
    error = (result.meta or {}).get(ERROR_META_KEY)
    if isinstance(error, Mapping):
        code = error.get("code")
        return code if isinstance(code, str) else None
    return None


def text_of(result: CallToolResult) -> str:
    """All text blocks of a result, joined by newlines."""
    return "\n".join(block.text for block in result.content if isinstance(block, TextContent))

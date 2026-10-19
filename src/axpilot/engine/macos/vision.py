"""Text recognition (OCR) with the Vision framework.

Permission: none (the capture being read already needed Screen Recording).

The capture's PNG/JPEG bytes are decoded with ImageIO (``CGImageSourceCreateWithData``) and
passed to ``VNRecognizeTextRequest`` at the accurate recognition level with language
correction. Vision reports bounding boxes normalized to 0…1 with the origin at the
*bottom*-left of the image; they are converted to global screen points with the capture's
``region``, so they can be clicked directly. Recognition runs synchronously on the calling
worker thread.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import Foundation
import Quartz as Q

from axpilot.engine.macos import helpers as h
from axpilot.engine.macos.sync import ns_error_text
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import Capture, OcrLine


def _vision() -> Any:
    try:
        import Vision
    except ImportError as exc:
        raise AXPilotError(
            ErrorCode.ENGINE_UNAVAILABLE,
            f"The Vision framework bindings are missing: {exc}",
            hint="Reinstall AXPilot on macOS: `uv tool install --reinstall axpilot`.",
        ) from exc
    return Vision


def _cg_image(capture: Capture) -> Any:
    if not capture.data:
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "The capture has no image data.")
    data = Foundation.NSData.dataWithBytes_length_(capture.data, len(capture.data))
    source = Q.CGImageSourceCreateWithData(data, None)
    image = Q.CGImageSourceCreateImageAtIndex(source, 0, None) if source is not None else None
    if image is None:
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "The capture data is not a readable image.")
    return image


def _configure(Vision: Any, request: Any, languages: Sequence[str] | None) -> None:
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(True)
    if languages:
        supported, error = request.supportedRecognitionLanguagesAndReturnError_(None)
        if error is not None or supported is None:
            raise AXPilotError(
                ErrorCode.INTERNAL, f"Vision cannot list its languages: {ns_error_text(error)}"
            )
        request.setRecognitionLanguages_(h.resolve_languages(languages, [str(code) for code in supported]))
    elif request.respondsToSelector_("setAutomaticallyDetectsLanguage:"):
        request.setAutomaticallyDetectsLanguage_(True)


def recognize_text(capture: Capture, languages: Sequence[str] | None = None) -> list[OcrLine]:
    """Recognized lines of text in ``capture``, top-to-bottom then left-to-right."""
    Vision = _vision()
    image = _cg_image(capture)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    _configure(Vision, request, languages)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, {})
    ok, error = handler.performRequests_error_([request], None)
    if not ok:
        raise AXPilotError(ErrorCode.INTERNAL, f"Text recognition failed: {ns_error_text(error)}")
    lines: list[OcrLine] = []
    for observation in list(request.results() or ()):
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        best = candidates[0]
        text = str(best.string()).strip()
        if not text:
            continue
        box = observation.boundingBox()
        frame = h.normalized_box_to_rect(
            float(box.origin.x),
            float(box.origin.y),
            float(box.size.width),
            float(box.size.height),
            capture.region,
        )
        lines.append(OcrLine(text=text, confidence=float(best.confidence()), frame=frame))
    lines.sort(key=lambda line: (round(line.frame.y), line.frame.x))
    return lines

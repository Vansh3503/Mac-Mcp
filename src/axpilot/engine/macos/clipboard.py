"""The general pasteboard: text, images (as PNG) and file URLs.

Permission: none. (macOS 15.4+ may show a paste-privacy prompt to the user for programmatic
reads, depending on their settings; that prompt belongs to macOS.)

Privacy: password managers mark copied secrets with ``org.nspasteboard.ConcealedType``
(see nspasteboard.org). AXPilot honours that marker and never returns the text of such
items; the type list still tells the caller that something concealed is there.

Images are returned as PNG: ``public.png`` data as is, otherwise TIFF data converted with
``NSBitmapImageRep``. Writing an image puts both PNG and TIFF representations on the
pasteboard, so older apps can paste it too.

:func:`save_clipboard` and :func:`restore_clipboard` copy the pasteboard byte for byte around a
paste, so nothing the user copied is lost (the other functions only know text, PNG and files).

Threading: reading the pasteboard may make the copying app fulfil a promise, which macOS wants on
the main thread ("synchronous promise fulfillment requested from a background thread"). Every
function here therefore does its pasteboard work on the main run loop
(:class:`~axpilot.engine.macos.helpers.MainThreadCaller`), which the CLI, host and SDK keep
turning; if it does not answer within :data:`MAIN_THREAD_START_S`, the work runs on the calling
thread as before, and the job left on the main run loop is cancelled so it can never run late.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import AppKit
import Foundation
from PyObjCTools import AppHelper

from axpilot.engine.macos import helpers as h
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import ClipboardContent

_log = logging.getLogger(__name__)

CONCEALED_TYPE = "org.nspasteboard.ConcealedType"
MAX_FILES = 1000
MAIN_THREAD_START_S = 0.5
"""How long pasteboard work waits for the main run loop (turned every 50 ms) to pick it up."""
MAIN_THREAD_FINISH_S = 10.0
"""How long pasteboard work on the main thread may take (an app providing a large copied item)."""


def _is_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


_MAIN = h.MainThreadCaller(
    AppHelper.callAfter,
    _is_main_thread,
    start_s=MAIN_THREAD_START_S,
    finish_s=MAIN_THREAD_FINISH_S,
    what="The clipboard",
)


def _pasteboard() -> Any:
    return AppKit.NSPasteboard.generalPasteboard()


def _nsdata(data: bytes) -> Any:
    return Foundation.NSData.dataWithBytes_length_(data, len(data))


def _png_from(pasteboard: Any) -> bytes | None:
    png = pasteboard.dataForType_(AppKit.NSPasteboardTypePNG)
    if png is not None and png.length() > 0:
        return bytes(png)
    tiff = pasteboard.dataForType_(AppKit.NSPasteboardTypeTIFF)
    if tiff is None or tiff.length() == 0:
        return None
    rep = AppKit.NSBitmapImageRep.imageRepWithData_(tiff)
    if rep is None:
        _log.warning("the clipboard holds TIFF data that cannot be decoded")
        return None
    converted = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    return bytes(converted) if converted is not None else None


def _files_from(pasteboard: Any) -> list[str]:
    options = {AppKit.NSPasteboardURLReadingFileURLsOnlyKey: True}
    urls = pasteboard.readObjectsForClasses_options_([AppKit.NSURL], options)
    paths = []
    for url in list(urls or ())[:MAX_FILES]:
        path = url.path()
        if path:
            paths.append(str(path))
    return paths


def read_clipboard() -> ClipboardContent:
    """What the general pasteboard holds (concealed text is withheld, see module doc)."""
    return _MAIN.call(_read_clipboard)


def _read_clipboard() -> ClipboardContent:
    pasteboard = _pasteboard()
    types = [str(t) for t in list(pasteboard.types() or ())]
    text = None
    if CONCEALED_TYPE not in types:
        value = pasteboard.stringForType_(AppKit.NSPasteboardTypeString)
        text = str(value) if value is not None else None
    return ClipboardContent(
        text=text, image_png=_png_from(pasteboard), files=_files_from(pasteboard), types=types
    )


def _validated_files(files: Sequence[str]) -> list[str]:
    if len(files) > MAX_FILES:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT, f"At most {MAX_FILES} files can be put on the clipboard."
        )
    paths = []
    for raw in files:
        path = Path(raw).expanduser()
        if not path.exists():
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"File not found: {path}")
        paths.append(str(path.resolve()))
    return paths


def write_clipboard(
    *, text: str | None = None, image_png: bytes | None = None, files: Sequence[str] | None = None
) -> None:
    """Replace the pasteboard contents. With nothing given, the pasteboard is cleared."""
    if image_png is not None and not h.is_png(image_png):
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "image_png is not PNG data.")
    paths = _validated_files(files or [])
    _MAIN.call(lambda: _write_clipboard(text, image_png, paths))


def _write_clipboard(text: str | None, image_png: bytes | None, paths: list[str]) -> None:
    objects: list[Any] = []
    if text is not None or image_png is not None:
        item = AppKit.NSPasteboardItem.alloc().init()
        if text is not None:
            item.setString_forType_(text, AppKit.NSPasteboardTypeString)
        if image_png is not None:
            png = _nsdata(image_png)
            item.setData_forType_(png, AppKit.NSPasteboardTypePNG)
            rep = AppKit.NSBitmapImageRep.imageRepWithData_(png)
            tiff = rep.TIFFRepresentation() if rep is not None else None
            if tiff is not None:
                item.setData_forType_(tiff, AppKit.NSPasteboardTypeTIFF)
        objects.append(item)
    objects.extend(AppKit.NSURL.fileURLWithPath_(path) for path in paths)
    pasteboard = _pasteboard()
    pasteboard.clearContents()
    if objects and not pasteboard.writeObjects_(objects):
        raise AXPilotError(ErrorCode.INTERNAL, "macOS refused to write to the clipboard.")


SavedClipboard = list[list[tuple[str, bytes]]]
"""Every pasteboard item as ``(type, raw data)`` pairs, in order."""


def save_clipboard() -> SavedClipboard:
    """A faithful copy of the pasteboard: every item with all its types (rich text, file references
    to missing files, private and concealed types), for :func:`restore_clipboard`."""
    return _MAIN.call(_save_clipboard)


def _save_clipboard() -> SavedClipboard:
    items: SavedClipboard = []
    for item in list(_pasteboard().pasteboardItems() or ()):
        entries = []
        for kind in list(item.types() or ()):
            data = item.dataForType_(kind)
            if data is not None:
                entries.append((str(kind), bytes(data)))
        items.append(entries)
    return items


def restore_clipboard(saved: SavedClipboard) -> None:
    """Put back what :func:`save_clipboard` copied (an empty copy clears the pasteboard)."""
    _MAIN.call(lambda: _restore_clipboard(saved))


def _restore_clipboard(saved: SavedClipboard) -> None:
    objects = []
    for entries in saved:
        item = AppKit.NSPasteboardItem.alloc().init()
        for kind, data in entries:
            item.setData_forType_(_nsdata(data), kind)
        objects.append(item)
    pasteboard = _pasteboard()
    pasteboard.clearContents()
    if objects and not pasteboard.writeObjects_(objects):
        raise AXPilotError(ErrorCode.INTERNAL, "macOS refused to restore the clipboard.")

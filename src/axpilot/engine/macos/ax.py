"""A thin, typed wrapper over the Accessibility (HIServices) C API.

Permission: every call here needs **Accessibility** (System Settings → Privacy & Security →
Accessibility) for the responsible app; without it AX returns ``kAXErrorAPIDisabled``.

PyObjC exposes the AX functions with their out-parameters returned as a tuple, e.g.
``AXUIElementCopyAttributeValue(element, "AXRole", None) -> (err, value)``. :class:`AX`
hides that, applies one messaging timeout to the whole process, tells timeouts apart from
other ``kAXErrorCannotComplete`` failures, retries a fast ``CannotComplete`` read once
(QUALITY A6), and raises :class:`AXCallError` for everything else. Callers turn those into
:class:`~axpilot.errors.AXPilotError` with :func:`ax_failure`, adding which element and app
they were working on.

Thread safety: AX calls are plain Mach messages to the target app and are safe from any
thread; ``AXUIElementRef`` objects can be shared between threads.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from typing import Any

import CoreFoundation as CF
import Foundation
import HIServices as HI

from axpilot.engine.macos import helpers as h
from axpilot.errors import AXPilotError, ErrorCode

_log = logging.getLogger(__name__)

AXElement = Any
"""An ``AXUIElementRef`` (PyObjC CF proxy). Kept as ``Any``: PyObjC types are dynamic."""


class AXCallError(Exception):
    """An AX call failed. Internal to the macOS engine; never escapes :class:`MacEngine`."""

    def __init__(
        self,
        code: int,
        operation: h.AXOperation,
        *,
        name: str | None = None,
        timed_out: bool = False,
    ) -> None:
        super().__init__(f"{operation} {name or ''} failed: {h.ax_error_name(code)}".strip())
        self.code = code
        self.operation: h.AXOperation = operation
        self.name = name
        self.timed_out = timed_out

    @property
    def error_code(self) -> ErrorCode:
        """The AXPilot error code this failure maps to."""
        return h.classify_ax_error(self.code, self.operation)

    @property
    def stale(self) -> bool:
        return self.code == h.AX_INVALID_UI_ELEMENT

    @property
    def permission(self) -> bool:
        return self.code == h.AX_API_DISABLED


_REASONS: dict[int, str] = {
    h.AX_API_DISABLED: "Accessibility permission is not granted",
    h.AX_INVALID_UI_ELEMENT: "the element no longer exists",
    h.AX_ACTION_UNSUPPORTED: "the element does not support that action",
    h.AX_ATTRIBUTE_UNSUPPORTED: "the element does not have that attribute",
    h.AX_NO_VALUE: "the element has no value",
    h.AX_NOT_IMPLEMENTED: "the app does not fully implement accessibility",
    h.AX_ILLEGAL_ARGUMENT: "the app rejected the value",
    h.AX_FAILURE: "the app reported a failure",
}

_HINTS: dict[ErrorCode, str] = {
    ErrorCode.TIMEOUT: "The app may be busy or showing a modal dialog. Wait a moment, then take a snapshot.",
    ErrorCode.ELEMENT_NOT_ACTIONABLE: "Use `inspect` to see what the element supports, or try `click`.",
}


def ax_failure(exc: AXCallError, what: str, *, timeout_s: float | None = None) -> AXPilotError:
    """Translate an :class:`AXCallError` into an :class:`AXPilotError`.

    ``what`` names the operation and target, e.g. ``"Reading the window of Safari"``.
    """
    code = exc.error_code
    if exc.code == h.AX_CANNOT_COMPLETE:
        if exc.timed_out and timeout_s:
            reason = f"the app did not respond within {timeout_s:g} s"
        else:
            reason = "the app did not answer the accessibility request"
    else:
        reason = _REASONS.get(exc.code, h.ax_error_name(exc.code))
    details: dict[str, Any] = {"ax_error": h.ax_error_name(exc.code)}
    if exc.name:
        details["ax_name"] = exc.name
    return AXPilotError(code, f"{what} failed: {reason}.", hint=_HINTS.get(code), details=details)


def _is_null(value: Any) -> bool:
    return value is None or isinstance(value, Foundation.NSNull)


class AX:
    """Accessibility calls with one process-wide messaging timeout."""

    def __init__(self, timeout_s: float = 1.0) -> None:
        if not timeout_s > 0:
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"ax_timeout_s must be > 0, got {timeout_s!r}.")
        self.timeout_s = float(timeout_s)
        self._system = HI.AXUIElementCreateSystemWide()
        # On the system-wide element this sets the default for every AX call of the process.
        HI.AXUIElementSetMessagingTimeout(self._system, self.timeout_s)
        self._element_type_id = HI.AXUIElementGetTypeID()
        self._value_type_id = HI.AXValueGetTypeID()

    # ------------------------------------------------------------------ elements
    def is_trusted(self) -> bool:
        """``AXIsProcessTrusted()``: whether Accessibility is granted. Never prompts."""
        return bool(HI.AXIsProcessTrusted())

    @property
    def system_wide(self) -> AXElement:
        return self._system

    def application(self, pid: int) -> AXElement:
        """The application element of ``pid`` (the call itself never fails)."""
        element = HI.AXUIElementCreateApplication(int(pid))
        HI.AXUIElementSetMessagingTimeout(element, self.timeout_s)
        return element

    def set_timeout(self, element: AXElement, seconds: float) -> None:
        """Messaging timeout for this element reference only."""
        HI.AXUIElementSetMessagingTimeout(element, float(max(seconds, 0.05)))

    def is_element(self, value: Any) -> bool:
        if _is_null(value) or isinstance(value, str | bytes | int | float | list | tuple | dict):
            return False
        try:
            return bool(CF.CFGetTypeID(value) == self._element_type_id)
        except (TypeError, ValueError):
            return False

    def is_value(self, value: Any) -> bool:
        if _is_null(value) or isinstance(value, str | bytes | int | float | list | tuple | dict):
            return False
        try:
            return bool(CF.CFGetTypeID(value) == self._value_type_id)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def equal(a: AXElement, b: AXElement) -> bool:
        """``CFEqual`` (a local comparison; no message is sent to the app)."""
        if a is None or b is None:
            return False
        try:
            return bool(CF.CFEqual(a, b))
        except (TypeError, ValueError):
            return False

    # ------------------------------------------------------------------ plumbing
    def _call(self, func: Callable[..., Any], *args: Any) -> tuple[int, Any, float]:
        start = time.monotonic()
        result = func(*args)
        elapsed = time.monotonic() - start
        if isinstance(result, tuple):
            err = int(result[0])
            value = result[1] if len(result) > 1 else None
        else:
            err, value = int(result), None
        return err, value, elapsed

    def _timed_out(self, err: int, elapsed: float) -> bool:
        return err == h.AX_CANNOT_COMPLETE and h.is_timeout(elapsed, self.timeout_s)

    def _read(self, func: Callable[..., Any], *args: Any, name: str) -> Any:
        """Run a read; ``None`` for missing values, one retry for a fast CannotComplete."""
        for attempt in range(2):
            err, value, elapsed = self._call(func, *args)
            if err == h.AX_SUCCESS:
                return None if _is_null(value) else value
            if err in h.MISSING_VALUE_ERRORS:
                return None
            timed_out = self._timed_out(err, elapsed)
            if err == h.AX_CANNOT_COMPLETE and not timed_out and attempt == 0:
                _log.debug("AX read %s: CannotComplete after %.3f s, retrying once", name, elapsed)
                continue
            raise AXCallError(err, "read", name=name, timed_out=timed_out)
        raise AssertionError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------------ reads
    def get(self, element: AXElement, attribute: str) -> Any:
        """One attribute value, or ``None`` if the element has none."""
        return self._read(HI.AXUIElementCopyAttributeValue, element, attribute, None, name=attribute)

    def get_many(self, element: AXElement, attributes: Sequence[str]) -> dict[str, Any]:
        """Several attributes in one round trip (``AXUIElementCopyMultipleAttributeValues``).

        Attributes the element lacks come back from AX as ``kAXValueAXErrorType`` placeholders;
        they are left out of the result, except ``kAXErrorAPIDisabled`` / ``InvalidUIElement``
        placeholders, which raise like a failed call.
        """
        names = list(attributes)
        if not names:
            return {}
        if len(names) == 1:
            value = self.get(element, names[0])
            return {} if value is None else {names[0]: value}
        values = self._read(HI.AXUIElementCopyMultipleAttributeValues, element, names, 0, None, name="*")
        result: dict[str, Any] = {}
        if values is None:
            # The app does not implement batched reads: fall back to one message per attribute.
            for name in names:
                value = self.get(element, name)
                if value is not None:
                    result[name] = value
            return result
        for name, value in zip(names, list(values), strict=False):
            if _is_null(value):
                continue
            if self.is_value(value):
                error = self.value_error(value)
                if error is not None:
                    if error in (h.AX_API_DISABLED, h.AX_INVALID_UI_ELEMENT):
                        raise AXCallError(error, "read", name=name)
                    continue
            result[name] = value
        return result

    def count(self, element: AXElement, attribute: str) -> int | None:
        """``AXUIElementGetAttributeValueCount`` (``None`` if unsupported)."""
        value = self._read(HI.AXUIElementGetAttributeValueCount, element, attribute, None, name=attribute)
        return int(value) if isinstance(value, int) else None

    def get_slice(self, element: AXElement, attribute: str, index: int, max_values: int) -> list[Any]:
        """``AXUIElementCopyAttributeValues``: part of an array attribute."""
        values = self._read(
            HI.AXUIElementCopyAttributeValues,
            element,
            attribute,
            int(index),
            int(max_values),
            None,
            name=attribute,
        )
        return [] if values is None else list(values)

    def parameterized(self, element: AXElement, attribute: str, parameter: Any) -> Any:
        """``AXUIElementCopyParameterizedAttributeValue`` (``None`` if unsupported)."""
        return self._read(
            HI.AXUIElementCopyParameterizedAttributeValue, element, attribute, parameter, None, name=attribute
        )

    def attribute_names(self, element: AXElement) -> list[str]:
        names = self._read(HI.AXUIElementCopyAttributeNames, element, None, name="AXAttributeNames")
        return [str(n) for n in names] if names is not None else []

    def actions(self, element: AXElement) -> tuple[str, ...]:
        names = self._read(HI.AXUIElementCopyActionNames, element, None, name="AXActionNames")
        return tuple(str(n) for n in names) if names is not None else ()

    def settable(self, element: AXElement, attribute: str) -> bool:
        """Whether ``attribute`` can be set (``False`` when unsupported)."""
        value = self._read(HI.AXUIElementIsAttributeSettable, element, attribute, None, name=attribute)
        return bool(value)

    def pid(self, element: AXElement) -> int | None:
        err, value, _ = self._call(HI.AXUIElementGetPid, element, None)
        return int(value) if err == h.AX_SUCCESS and value else None

    def element_at(self, x: float, y: float, app: AXElement | None = None) -> AXElement | None:
        """The element under a global point (``AXUIElementCopyElementAtPosition``)."""
        target = app if app is not None else self._system
        return self._read(
            HI.AXUIElementCopyElementAtPosition, target, float(x), float(y), None, name="position"
        )

    # ------------------------------------------------------------------ writes
    def perform(self, element: AXElement, action: str) -> None:
        """Perform an action. Raises :class:`AXCallError` (``timed_out`` set when the app did
        not answer in time; the action was usually delivered, e.g. it opened a modal)."""
        err, _, elapsed = self._call(HI.AXUIElementPerformAction, element, action)
        if err != h.AX_SUCCESS:
            raise AXCallError(err, "action", name=action, timed_out=self._timed_out(err, elapsed))

    def set(self, element: AXElement, attribute: str, value: Any) -> None:
        """Set an attribute. Raises :class:`AXCallError` on failure."""
        err, _, elapsed = self._call(HI.AXUIElementSetAttributeValue, element, attribute, value)
        if err != h.AX_SUCCESS:
            raise AXCallError(err, "write", name=attribute, timed_out=self._timed_out(err, elapsed))

    # ------------------------------------------------------------------ AXValue
    def value_type(self, value: Any) -> int:
        return int(HI.AXValueGetType(value))

    def value_error(self, value: Any) -> int | None:
        """The ``AXError`` inside a ``kAXValueAXErrorType`` value, else ``None``."""
        if self.value_type(value) != h.AXVALUE_AXERROR:
            return None
        decoded = self.decode_value(value)
        return int(decoded[1][0]) if decoded is not None else h.AX_FAILURE

    def decode_value(self, value: Any) -> tuple[int, tuple[float, ...]] | None:
        """Decode an ``AXValueRef`` into ``(type, numbers)``.

        Points give ``(x, y)``, sizes ``(w, h)``, rects ``(x, y, w, h)``, ranges
        ``(location, length)`` and errors ``(code,)``. Falls back to parsing the value's
        description if ``AXValueGetValue`` cannot decode it.
        """
        value_type = self.value_type(value)
        try:
            ok, raw = HI.AXValueGetValue(value, value_type, None)
        except (TypeError, ValueError) as exc:
            _log.debug("AXValueGetValue failed for type %s: %s", value_type, exc)
            ok, raw = False, None
        if ok and raw is not None:
            numbers = _struct_numbers(value_type, raw)
            if numbers is not None:
                return value_type, numbers
        return h.parse_axvalue_description(str(value))

    def make_point(self, x: float, y: float) -> Any:
        """An ``AXValueRef`` holding a ``CGPoint`` (for ``AXPosition``)."""
        return self._make_value(h.AXVALUE_CGPOINT, (float(x), float(y)))

    def make_size(self, width: float, height: float) -> Any:
        """An ``AXValueRef`` holding a ``CGSize`` (for ``AXSize``)."""
        return self._make_value(h.AXVALUE_CGSIZE, (float(width), float(height)))

    def make_range(self, location: int, length: int) -> Any:
        """An ``AXValueRef`` holding a ``CFRange`` (for ``AXStringForRange``)."""
        return self._make_value(h.AXVALUE_CFRANGE, (int(location), int(length)))

    @staticmethod
    def _make_value(value_type: int, struct: tuple[float, ...]) -> Any:
        # PyObjC's manual AXValueCreate wrapper converts a tuple to the C struct.
        try:
            created = HI.AXValueCreate(value_type, struct)
        except (TypeError, ValueError) as exc:
            raise AXPilotError(ErrorCode.INTERNAL, f"AXValueCreate failed: {exc}") from exc
        if created is None:
            raise AXPilotError(
                ErrorCode.INTERNAL, f"AXValueCreate returned NULL for value type {value_type}."
            )
        return created


def _struct_numbers(value_type: int, raw: Any) -> tuple[float, ...] | None:
    """Numbers out of the PyObjC struct returned by ``AXValueGetValue``."""
    try:
        if value_type == h.AXVALUE_CGPOINT:
            return float(raw.x), float(raw.y)
        if value_type == h.AXVALUE_CGSIZE:
            return float(raw.width), float(raw.height)
        if value_type == h.AXVALUE_CGRECT:
            return float(raw.origin.x), float(raw.origin.y), float(raw.size.width), float(raw.size.height)
        if value_type == h.AXVALUE_CFRANGE:
            return float(raw.location), float(raw.length)
        if value_type == h.AXVALUE_AXERROR:
            return (float(int(raw)),)
    except (AttributeError, TypeError, ValueError):
        return None
    return None

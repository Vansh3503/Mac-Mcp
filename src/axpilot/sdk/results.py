"""Typed results of the automation SDK, parsed from the tools' structured content.

Every result keeps the tool's full structured content in :attr:`ToolResult.data` (the same
fields MCP clients see) and its text in :attr:`ToolResult.text`; the typed attributes are
shortcuts. Results that contain elements hand out *handles* (:class:`ElementHandle` for
:class:`~axpilot.sdk.Desktop`, :class:`AsyncElementHandle` for
:class:`~axpilot.sdk.AsyncDesktop`) that act on the element by ref.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from axpilot.errors import AXPilotError, ErrorCode

if TYPE_CHECKING:
    from axpilot.sdk.desktop import AsyncDesktop, Desktop

__all__ = [
    "ActionResult",
    "AppHandle",
    "AsyncAppHandle",
    "AsyncElementHandle",
    "Element",
    "ElementHandle",
    "FindResult",
    "ReadResult",
    "ScreenshotResult",
    "SnapshotResult",
    "ToolResult",
]

H = TypeVar("H")
HandleFactory = Callable[[str, "str | None", "Element | None"], Any]
"""``(ref, snapshot_id, element) → handle``; the desktop decides the handle type."""


@dataclass(frozen=True, slots=True)
class Element:
    """One element of a snapshot (or a find match)."""

    ref: str
    role: str
    name: str | None = None
    value: str | None = None
    states: tuple[str, ...] = ()
    center: tuple[float, float] | None = None
    frame: tuple[float, float, float, float] | None = None
    actions: tuple[str, ...] = ()

    @classmethod
    def from_payload(cls, data: Mapping[str, Any]) -> Element:
        center, frame = data.get("center"), data.get("frame")
        return cls(
            ref=str(data["ref"]),
            role=str(data.get("role", "")),
            name=data.get("name"),
            value=data.get("value"),
            states=tuple(data.get("states") or ()),
            center=(float(center[0]), float(center[1])) if center else None,
            frame=tuple(float(v) for v in frame) if frame else None,  # type: ignore[arg-type]
            actions=tuple(data.get("actions") or ()),
        )

    @property
    def enabled(self) -> bool:
        return "disabled" not in self.states

    @property
    def focused(self) -> bool:
        return "focused" in self.states

    @property
    def selected(self) -> bool:
        return "selected" in self.states

    @property
    def checked(self) -> bool:
        return "checked" in self.states


@dataclass(slots=True)
class ToolResult:
    """The outcome of one tool call."""

    tool: str
    data: dict[str, Any] = field(default_factory=dict)
    """The tool's structured content."""
    text: str = ""
    images: list[bytes] = field(default_factory=list)

    @property
    def warnings(self) -> list[str]:
        return list(self.data.get("warnings") or [])

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


@dataclass(slots=True)
class ActionResult(ToolResult):
    """The outcome of an input action: how it was done and what changed."""

    @property
    def message(self) -> str:
        """What was done, e.g. ``clicked e3 button "New Note" via ax:AXPress``."""
        return self.data.get("message") or (self.text.splitlines()[0] if self.text else "")

    @property
    def strategy(self) -> str | None:
        return self.data.get("strategy")

    @property
    def snapshot_id(self) -> str | None:
        return self.data.get("snapshot_id")

    @property
    def diff(self) -> dict[str, Any] | None:
        return self.data.get("diff")

    @property
    def changed(self) -> bool:
        """True if the action's diff shows a change (``False`` without a diff)."""
        diff = self.diff
        return diff is not None and not diff.get("empty", True)


@dataclass(slots=True)
class SnapshotResult(ToolResult, Generic[H]):
    """An app's UI as elements with refs."""

    handles: HandleFactory | None = None

    @property
    def snapshot_id(self) -> str:
        return str(self.data["snapshot_id"])

    @property
    def app(self) -> str:
        return str(self.data["app"]["name"])

    @property
    def window(self) -> str | None:
        window = self.data.get("window")
        return window.get("title") if window else None

    @property
    def elements(self) -> list[Element]:
        return [Element.from_payload(item) for item in self.data.get("elements", [])]

    @property
    def focused(self) -> Element | None:
        ref = self.data.get("focused_ref")
        return next((el for el in self.elements if el.ref == ref), None) if ref else None

    @property
    def next_offset(self) -> int | None:
        return self.data.get("next_offset")

    def __iter__(self) -> Iterator[Element]:
        return iter(self.elements)

    def __len__(self) -> int:
        return len(self.data.get("elements", []))

    def element(self, ref: str) -> H:
        """A handle on the element with ``ref``. Raises ``STALE_REF`` if it is not listed."""
        found = next((el for el in self.elements if el.ref == ref), None)
        if found is None:
            raise AXPilotError(
                ErrorCode.STALE_REF,
                f"{ref} is not in snapshot {self.snapshot_id}.",
                hint="Take a new snapshot.",
            )
        return self._handle(found)

    def find(self, name: str | None = None, *, role: str | None = None, exact: bool = False) -> H | None:
        """The first element whose name contains ``name`` (case-insensitive; ``exact`` for equality)
        and whose role is ``role``, as a handle; ``None`` if nothing matches."""
        for el in self.find_all(name, role=role, exact=exact):
            return self._handle(el)
        return None

    def find_all(
        self, name: str | None = None, *, role: str | None = None, exact: bool = False
    ) -> list[Element]:
        """Every element matching ``name`` and ``role`` (see :meth:`find`)."""
        wanted = name.casefold() if name is not None else None
        out: list[Element] = []
        for el in self.elements:
            if role is not None and el.role != role:
                continue
            if wanted is not None:
                have = (el.name or "").casefold()
                if (have != wanted) if exact else (wanted not in have):
                    continue
            out.append(el)
        return out

    def _handle(self, el: Element) -> H:
        assert self.handles is not None
        return self.handles(el.ref, self.snapshot_id, el)


@dataclass(slots=True)
class FindResult(ToolResult, Generic[H]):
    """Matches of ``find``, best first, as handles."""

    handles: HandleFactory | None = None

    @property
    def found(self) -> bool:
        return bool(self.data.get("found"))

    @property
    def snapshot_id(self) -> str:
        return str(self.data["snapshot_id"])

    @property
    def matches(self) -> list[H]:
        assert self.handles is not None
        sid = self.snapshot_id
        return [self.handles(m["ref"], sid, Element.from_payload(m)) for m in self.data.get("matches", [])]

    @property
    def first(self) -> H:
        """The best match. Raises ``INVALID_ARGUMENT`` when nothing good matched."""
        matches = self.matches
        if not self.found or not matches:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                "Nothing matched well enough.",
                hint="Check the text, pass scroll=True to search a long list, or take a snapshot.",
            )
        return matches[0]

    def __getattr__(self, name: str) -> Any:
        """``find(...).click()`` acts on the best match (any handle method works)."""
        if name.startswith("_") or name in ("handles", "tool", "data", "text", "images"):
            raise AttributeError(name)
        return getattr(self.first, name)


@dataclass(slots=True)
class ReadResult(ToolResult):
    """The whole content of a list, table, document or page."""

    @property
    def content(self) -> str:
        return str(self.data.get("content", ""))

    @property
    def lines(self) -> list[str]:
        return list(self.data.get("lines") or [])

    @property
    def rows(self) -> list[str]:
        """The lines (rows in ``mode="rows"``)."""
        return self.lines

    @property
    def complete(self) -> bool:
        return bool(self.data.get("complete"))

    @property
    def at_end(self) -> bool:
        return bool(self.data.get("at_end"))

    @property
    def pages(self) -> int:
        return int(self.data.get("pages", 0))


@dataclass(slots=True)
class ScreenshotResult(ToolResult):
    """A screenshot: the image bytes and how its pixels map to screen points."""

    @property
    def screenshot_id(self) -> str:
        return str(self.data["screenshot_id"])

    @property
    def width(self) -> int:
        return int(self.data["width"])

    @property
    def height(self) -> int:
        return int(self.data["height"])

    @property
    def format(self) -> str:
        return str(self.data.get("format", "png"))

    @property
    def image(self) -> bytes:
        if not self.images:
            raise AXPilotError(ErrorCode.INTERNAL, "The screenshot result carries no image.")
        return self.images[0]

    def save(self, path: str | Path) -> Path:
        """Write the image to ``path`` and return it."""
        target = Path(path).expanduser()
        target.write_bytes(self.image)
        return target


# =========================================================================== handles
class ElementHandle:
    """An element of a :class:`~axpilot.sdk.Desktop` snapshot, acted on by ref."""

    def __init__(
        self, desktop: Desktop, ref: str, snapshot_id: str | None, element: Element | None = None
    ) -> None:
        self.desktop = desktop
        self.ref = ref
        self.snapshot_id = snapshot_id
        self.element = element

    def __repr__(self) -> str:
        name = f" {self.element.role} {self.element.name!r}" if self.element else ""
        return f"<ElementHandle {self.ref}{name}>"

    def click(self, **options: Any) -> ActionResult:
        return self.desktop.click(self.ref, snapshot_id=self.snapshot_id, **options)

    def type_text(self, text: str, **options: Any) -> ActionResult:
        return self.desktop.type_text(text, self.ref, snapshot_id=self.snapshot_id, **options)

    def set_value(self, value: str | bool | float) -> ActionResult:
        return self.desktop.set_value(self.ref, value, snapshot_id=self.snapshot_id)

    def perform_action(self, action: str) -> ActionResult:
        return self.desktop.perform_action(self.ref, action, snapshot_id=self.snapshot_id)

    def inspect(self) -> ToolResult:
        return self.desktop.inspect(self.ref, snapshot_id=self.snapshot_id)

    def scroll_into_view(self) -> ActionResult:
        return self.desktop.scroll_into_view(self.ref, snapshot_id=self.snapshot_id)


class AsyncElementHandle:
    """An element of an :class:`~axpilot.sdk.AsyncDesktop` snapshot, acted on by ref."""

    def __init__(
        self, desktop: AsyncDesktop, ref: str, snapshot_id: str | None, element: Element | None = None
    ) -> None:
        self.desktop = desktop
        self.ref = ref
        self.snapshot_id = snapshot_id
        self.element = element

    def __repr__(self) -> str:
        name = f" {self.element.role} {self.element.name!r}" if self.element else ""
        return f"<AsyncElementHandle {self.ref}{name}>"

    async def click(self, **options: Any) -> ActionResult:
        return await self.desktop.click(self.ref, snapshot_id=self.snapshot_id, **options)

    async def type_text(self, text: str, **options: Any) -> ActionResult:
        return await self.desktop.type_text(text, self.ref, snapshot_id=self.snapshot_id, **options)

    async def set_value(self, value: str | bool | float) -> ActionResult:
        return await self.desktop.set_value(self.ref, value, snapshot_id=self.snapshot_id)

    async def perform_action(self, action: str) -> ActionResult:
        return await self.desktop.perform_action(self.ref, action, snapshot_id=self.snapshot_id)

    async def inspect(self) -> ToolResult:
        return await self.desktop.inspect(self.ref, snapshot_id=self.snapshot_id)

    async def scroll_into_view(self) -> ActionResult:
        return await self.desktop.scroll_into_view(self.ref, snapshot_id=self.snapshot_id)


class AppHandle:
    """An app of a :class:`~axpilot.sdk.Desktop`, by name or bundle id."""

    def __init__(self, desktop: Desktop, name: str) -> None:
        self.desktop = desktop
        self.name = name

    def __repr__(self) -> str:
        return f"<AppHandle {self.name!r}>"

    def launch(self) -> AppHandle:
        self.desktop.app_action("launch", self.name)
        return self

    def activate(self) -> AppHandle:
        self.desktop.app_action("activate", self.name)
        return self

    def hide(self) -> AppHandle:
        self.desktop.app_action("hide", self.name)
        return self

    def quit(self) -> ToolResult:
        return self.desktop.app_action("quit", self.name)

    def windows(self) -> list[dict[str, Any]]:
        return self.desktop.list_windows(self.name)

    def snapshot(self, **options: Any) -> SnapshotResult[ElementHandle]:
        return self.desktop.snapshot(self.name, **options)


class AsyncAppHandle:
    """An app of an :class:`~axpilot.sdk.AsyncDesktop`, by name or bundle id."""

    def __init__(self, desktop: AsyncDesktop, name: str) -> None:
        self.desktop = desktop
        self.name = name

    def __repr__(self) -> str:
        return f"<AsyncAppHandle {self.name!r}>"

    async def launch(self) -> AsyncAppHandle:
        await self.desktop.app_action("launch", self.name)
        return self

    async def activate(self) -> AsyncAppHandle:
        await self.desktop.app_action("activate", self.name)
        return self

    async def hide(self) -> AsyncAppHandle:
        await self.desktop.app_action("hide", self.name)
        return self

    async def quit(self) -> ToolResult:
        return await self.desktop.app_action("quit", self.name)

    async def windows(self) -> list[dict[str, Any]]:
        return await self.desktop.list_windows(self.name)

    async def snapshot(self, **options: Any) -> SnapshotResult[AsyncElementHandle]:
        return await self.desktop.snapshot(self.name, **options)

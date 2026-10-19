"""App compatibility: what kind of app is this, how good is its accessibility tree, and how
should an agent drive it?

Mac apps expose very different accessibility (AX) trees. A native AppKit app describes every
control; an Electron app shows an empty window until asked for its web content; a game or a
canvas editor shows almost nothing. This module turns that into three answers:

* :func:`detect_app_kind` looks at the app bundle (frameworks, Info.plist, bundle id) and
  returns an :data:`AppKind` such as ``"electron"`` or ``"java"``. It only reads the file
  system through a :class:`BundleFS`, so tests (and the fake engine) can inject an in-memory
  bundle with :class:`MemoryBundleFS`.
* :func:`assess_ax_quality` grades a captured tree ``"good"``, ``"partial"`` or ``"poor"``
  from measurable facts (:class:`AxMetrics`); the thresholds are the ``*_RATIO`` / ``*_MIN``
  constants below.
* :func:`app_profile` combines both into an :class:`AppProfile`: the ordered strategies to use
  (see :data:`STRATEGIES`) and short notes written for the model.

Everything here is pure Python and never raises for unreadable bundles: missing files,
unreadable or malformed plists and file-system errors simply count as "not present".
"""

from __future__ import annotations

import logging
import os
import plistlib
import posixpath
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from axpilot.models import AppInfo, RawNode, RawTree
from axpilot.perception.roles import TEXT_INPUT_ROLES, is_interactive
from axpilot.perception.snapshot import WINDOW_FRAME_SUBROLES, node_name

log = logging.getLogger(__name__)

AppKind = Literal[
    "native",
    "catalyst",
    "ios",
    "electron",
    "cef",
    "chromium_browser",
    "safari",
    "firefox",
    "java",
    "qt",
    "office",
    "adobe",
    "unknown",
]
APP_KINDS: tuple[AppKind, ...] = (
    "native",
    "catalyst",
    "ios",
    "electron",
    "cef",
    "chromium_browser",
    "safari",
    "firefox",
    "java",
    "qt",
    "office",
    "adobe",
    "unknown",
)

AxQuality = Literal["good", "partial", "poor"]
AX_QUALITIES: tuple[AxQuality, ...] = ("poor", "partial", "good")
"""Quality grades from worst to best (index = rank)."""

Strategy = Literal[
    "ax_actions", "enhance_ax", "web_area", "browser_toolset", "menus_first", "vision_fallback", "coordinates"
]
STRATEGIES: dict[str, str] = {
    "ax_actions": "Use snapshot refs and AX actions (press, set_value) — background-safe and precise.",
    "enhance_ax": "AXPilot switches on the app's full accessibility tree (AXManualAccessibility).",
    "web_area": "Content lives in AXWebArea elements; read and act inside them.",
    "browser_toolset": "browser_tabs / browser_read / browser_eval_js work for this browser (when enabled).",
    "menus_first": "Prefer select_menu and keyboard shortcuts; menus are the most reliable surface.",
    "vision_fallback": 'OCR text becomes clickable o-refs (snapshot mode="vision", or automatic when poor).',
    "coordinates": "Click by screen coordinates (from a screenshot) as the last resort.",
}
"""Every strategy name an :class:`AppProfile` can list, with a one-line explanation."""

KIND_LABELS: dict[str, str] = {
    "native": "native AppKit/SwiftUI app",
    "catalyst": "Mac Catalyst (UIKit) app",
    "ios": "iPhone/iPad app on Apple silicon",
    "electron": "Electron app",
    "cef": "Chromium Embedded Framework app",
    "chromium_browser": "Chromium-based browser",
    "safari": "Safari",
    "firefox": "Gecko (Firefox family) app",
    "java": "Java (Swing/JavaFX) app",
    "qt": "Qt app",
    "office": "Microsoft Office app",
    "adobe": "Adobe app",
    "unknown": "unrecognized app",
}

# --------------------------------------------------------------------------- bundle-id tables
SAFARI_BUNDLE_IDS: frozenset[str] = frozenset({"com.apple.safari", "com.apple.safaritechnologypreview"})
"""Casefolded Safari bundle ids."""

CHROMIUM_BROWSER_PREFIXES: tuple[str, ...] = (
    "com.google.chrome",
    "company.thebrowser.",
    "com.microsoft.edgemac",
    "com.brave.browser",
    "org.chromium.chromium",
    "com.vivaldi.vivaldi",
    "com.operasoftware.opera",
)
"""Casefolded bundle-id prefixes of Chromium-based browsers (Chrome, Arc, Edge, Brave, Chromium,
Vivaldi, Opera and their beta/dev/canary channels)."""

BROWSER_TOOLSET_BUNDLE_IDS: frozenset[str] = frozenset(
    {
        "com.apple.safari",
        "com.apple.safaritechnologypreview",
        "com.google.chrome",
        "company.thebrowser.browser",
        "com.microsoft.edgemac",
        "com.brave.browser",
        "org.chromium.chromium",
        "com.vivaldi.vivaldi",
    }
)
"""Casefolded bundle ids the ``browser`` toolset can script (mirrors ``axpilot.system.browser.BROWSERS``)."""

FIREFOX_PREFIX = "org.mozilla."

OFFICE_PREFIXES: tuple[str, ...] = (
    "com.microsoft.word",
    "com.microsoft.excel",
    "com.microsoft.powerpoint",
    "com.microsoft.outlook",
    "com.microsoft.onenote",
)
"""Casefolded bundle-id prefixes of the Microsoft Office apps (not every ``com.microsoft.`` app:
VS Code and Teams are Electron/web apps and are detected from their bundles)."""

ADOBE_PREFIX = "com.adobe."

KNOWN_KINDS: dict[str, AppKind] = {
    "com.tinyspeck.slackmacgap": "electron",
    "com.microsoft.vscode": "electron",
    "com.microsoft.vscodeinsiders": "electron",
    "com.hnc.discord": "electron",
    "notion.id": "electron",
    "md.obsidian": "electron",
    "com.figma.desktop": "electron",
    "com.spotify.client": "cef",
    "com.google.android.studio": "java",
}
"""Casefolded bundle ids of well-known apps, used only when the bundle itself cannot be read."""

KNOWN_PREFIX_KINDS: tuple[tuple[str, AppKind], ...] = (("com.jetbrains.", "java"),)

APPLE_CATALYST_BUNDLE_IDS: frozenset[str] = frozenset(
    {"com.apple.news", "com.apple.stocks", "com.apple.home", "com.apple.voicememos", "com.apple.mobilesms"}
)
"""Casefolded ids of Apple's own Catalyst apps, used when the bundle cannot be read."""

# --------------------------------------------------------------------------- bundle-structure markers
_QT_FRAMEWORK = re.compile(r"^Qt[A-Z0-9]\w*\.framework$")
"""``QtCore.framework``, ``QtWidgets.framework``, ``Qt3DCore.framework`` (not Apple's ``QTKit``)."""
_QT_DYLIB = re.compile(r"^libQt\d*Core\b.*\.dylib$")
_CHROMIUM_FRAMEWORK_SUFFIX = " framework.framework"
_RENDERER_HELPER_SUFFIX = " helper (renderer).app"
_JAVA_LAUNCHERS: frozenset[str] = frozenset(
    {"javaapplauncher", "javaapplicationstub", "universaljavaapplicationstub"}
)
_JAVA_PLIST_KEYS: frozenset[str] = frozenset(
    {"JVMOptions", "JVMMainClassName", "JVMVersion", "Java", "JavaX"}
)
_UIKIT_PLIST_KEYS: frozenset[str] = frozenset(
    {
        "UIDeviceFamily",
        "UIApplicationSceneManifest",
        "UILaunchStoryboardName",
        "UIRequiredDeviceCapabilities",
        "UISupportedInterfaceOrientations",
        "UIApplicationSupportsIndirectInputEvents",
    }
)
_MAC_IDIOM = 6
"""``UIDeviceFamily`` value of the Mac idiom ("Optimize Interface for Mac" Catalyst apps)."""
_PLIST_MAX_BYTES = 4 * 1024 * 1024


# --------------------------------------------------------------------------- file-system abstraction
@runtime_checkable
class BundleFS(Protocol):
    """The three read-only file-system operations app-kind detection needs."""

    def exists(self, path: str) -> bool:
        """True if a file or directory exists at ``path`` (following symlinks)."""
        ...

    def listdir(self, path: str) -> list[str]:
        """Entry names of the directory at ``path`` (``[]`` if it is missing or not a directory)."""
        ...

    def read_plist(self, path: str) -> dict[str, Any] | None:
        """The property list at ``path`` as a dict, or ``None`` if missing or unreadable."""
        ...


class LocalBundleFS:
    """:class:`BundleFS` over the real file system (``os.path`` and ``plistlib``)."""

    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def listdir(self, path: str) -> list[str]:
        try:
            return sorted(os.listdir(path))
        except OSError:
            return []

    def read_plist(self, path: str) -> dict[str, Any] | None:
        try:
            if os.path.getsize(path) > _PLIST_MAX_BYTES:
                log.debug("Ignoring oversized plist %s", path)
                return None
            with open(path, "rb") as handle:
                data = plistlib.load(handle)
        except (OSError, ValueError) as exc:  # includes plistlib.InvalidFileException
            log.debug("Cannot read plist %s: %s", path, type(exc).__name__)
            return None
        except Exception as exc:  # malformed XML plists raise parser-specific errors
            log.debug("Malformed plist %s: %s", path, type(exc).__name__)
            return None
        return data if isinstance(data, dict) else None


LOCAL_FS: BundleFS = LocalBundleFS()
"""The default file system for :func:`detect_app_kind`."""


def _normpath(path: str) -> str:
    normalized = posixpath.normpath("/" + path.strip().lstrip("/"))
    return normalized


class MemoryBundleFS:
    """An in-memory :class:`BundleFS` for tests and the fake engine.

    ``files`` are absolute paths; a path ending in ``/`` (or any parent of another path) is a
    directory. ``plists`` maps absolute paths to their contents (those paths exist too)::

        MemoryBundleFS(
            ["/Applications/Slack.app/Contents/Frameworks/Electron Framework.framework/"],
            {"/Applications/Slack.app/Contents/Info.plist": {"CFBundlePackageType": "APPL"}},
        )
    """

    def __init__(
        self, files: Iterable[str] = (), plists: Mapping[str, Mapping[str, Any]] | None = None
    ) -> None:
        self._entries: set[str] = {"/"}
        self._children: dict[str, set[str]] = {}
        self._plists: dict[str, dict[str, Any]] = {}
        for path in files:
            self._add(path)
        for path, data in (plists or {}).items():
            self._plists[self._add(path)] = dict(data)

    def _add(self, path: str) -> str:
        normalized = _normpath(path)
        current = normalized
        while current != "/":
            parent, name = posixpath.split(current)
            self._entries.add(current)
            self._children.setdefault(parent, set()).add(name)
            current = parent
        return normalized

    def exists(self, path: str) -> bool:
        return _normpath(path) in self._entries

    def listdir(self, path: str) -> list[str]:
        return sorted(self._children.get(_normpath(path), ()))

    def read_plist(self, path: str) -> dict[str, Any] | None:
        data = self._plists.get(_normpath(path))
        return dict(data) if data is not None else None


class _Probe:
    """Wraps a :class:`BundleFS` so a failing implementation degrades to "not present"."""

    def __init__(self, fs: BundleFS) -> None:
        self._fs = fs

    def exists(self, path: str) -> bool:
        try:
            return bool(self._fs.exists(path))
        except Exception as exc:
            log.debug("Bundle probe exists(%s) failed: %s", path, type(exc).__name__)
            return False

    def listdir(self, path: str) -> list[str]:
        try:
            return [str(name) for name in self._fs.listdir(path) or ()]
        except Exception as exc:
            log.debug("Bundle probe listdir(%s) failed: %s", path, type(exc).__name__)
            return []

    def read_plist(self, path: str) -> dict[str, Any]:
        try:
            data = self._fs.read_plist(path)
        except Exception as exc:
            log.debug("Bundle probe read_plist(%s) failed: %s", path, type(exc).__name__)
            return {}
        return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- detection
def bundle_root(path: str | None) -> str | None:
    """The ``.app`` bundle that contains ``path`` (the innermost one), or ``path`` itself.

    ``"/Applications/Foo.app/Contents/MacOS/Foo"`` → ``"/Applications/Foo.app"``."""
    if not path or not path.strip():
        return None
    trimmed = path.strip().rstrip("/") or "/"
    folded = trimmed.casefold()
    if folded.endswith(".app"):
        return trimmed
    index = folded.rfind(".app/")
    return trimmed[: index + 4] if index >= 0 else trimmed


def _bundle_id_kind(bundle_id: str) -> AppKind | None:
    """Kinds that the bundle id alone decides (browsers, Office, Adobe)."""
    if bundle_id in SAFARI_BUNDLE_IDS:
        return "safari"
    if bundle_id.startswith(CHROMIUM_BROWSER_PREFIXES):
        return "chromium_browser"
    if bundle_id.startswith(FIREFOX_PREFIX):
        return "firefox"
    if bundle_id.startswith(OFFICE_PREFIXES):
        return "office"
    if bundle_id.startswith(ADOBE_PREFIX):
        return "adobe"
    return None


def _fallback_id_kind(bundle_id: str) -> AppKind | None:
    """Kinds guessed from well-known bundle ids when the bundle did not tell."""
    known = KNOWN_KINDS.get(bundle_id)
    if known is not None:
        return known
    for prefix, kind in KNOWN_PREFIX_KINDS:
        if bundle_id.startswith(prefix):
            return kind
    if bundle_id in APPLE_CATALYST_BUNDLE_IDS:
        return "catalyst"
    if bundle_id.startswith("com.apple."):
        return "native"
    return None


def _framework_kind(root: str, probe: _Probe) -> AppKind | None:
    frameworks_dir = posixpath.join(root, "Contents", "Frameworks")
    entries = probe.listdir(frameworks_dir)
    folded = {name.casefold() for name in entries}
    if "electron framework.framework" in folded:
        return "electron"
    if "chromium embedded framework.framework" in folded:
        return "cef"
    if any(_QT_FRAMEWORK.match(name) or _QT_DYLIB.match(name) for name in entries):
        return "qt"
    if probe.exists(posixpath.join(root, "Contents", "PlugIns", "platforms", "libqcocoa.dylib")):
        return "qt"
    for name in entries:
        if name.casefold().endswith(_CHROMIUM_FRAMEWORK_SUFFIX) and _has_renderer_helper(
            posixpath.join(frameworks_dir, name), probe
        ):
            return "chromium_browser"
    return None


def _has_renderer_helper(framework: str, probe: _Probe) -> bool:
    """Chromium ships ``<Name> Helper (Renderer).app`` inside its framework's Helpers folder."""
    candidates = [posixpath.join(framework, "Helpers")]
    versions = posixpath.join(framework, "Versions")
    candidates += [posixpath.join(versions, version, "Helpers") for version in probe.listdir(versions)]
    return any(
        name.casefold().endswith(_RENDERER_HELPER_SUFFIX)
        for helpers in candidates
        for name in probe.listdir(helpers)
    )


def _java_kind(root: str, probe: _Probe, info: Mapping[str, Any]) -> AppKind | None:
    contents = posixpath.join(root, "Contents")
    plugins = probe.listdir(posixpath.join(contents, "PlugIns"))
    if any("jdk" in name.casefold() or "jre" in name.casefold() for name in plugins):
        return "java"
    markers = (
        ("jbr",),  # JetBrains Runtime (IntelliJ IDEA, PyCharm, …)
        ("runtime", "Contents", "Home"),  # jpackage
        ("Java",),  # Oracle appbundler
        ("Resources", "Java"),  # Apple Jar Bundler
    )
    if any(probe.exists(posixpath.join(contents, *parts)) for parts in markers):
        return "java"
    launchers = {name.casefold() for name in probe.listdir(posixpath.join(contents, "MacOS"))}
    executable = str(info.get("CFBundleExecutable", "")).casefold()
    if launchers & _JAVA_LAUNCHERS or executable in _JAVA_LAUNCHERS:
        return "java"
    if _JAVA_PLIST_KEYS & set(info):
        return "java"
    return None


def _device_families(info: Mapping[str, Any]) -> list[int]:
    raw = info.get("UIDeviceFamily")
    values = raw if isinstance(raw, list) else [raw] if raw is not None else []
    families: list[int] = []
    for value in values:
        try:
            families.append(int(value))
        except (TypeError, ValueError):
            continue
    return families


def _platforms(info: Mapping[str, Any]) -> set[str]:
    raw = info.get("CFBundleSupportedPlatforms")
    values = raw if isinstance(raw, list) else [raw] if raw is not None else []
    platforms = {str(value).casefold() for value in values}
    if info.get("DTPlatformName"):
        platforms.add(str(info["DTPlatformName"]).casefold())
    return platforms


def _uikit_kind(info: Mapping[str, Any], *, mac_layout: bool) -> AppKind | None:
    """iOS vs Catalyst from Info.plist (the heuristic is documented in ``docs/app-compat.md``).

    * ``ios``: ``LSRequiresIPhoneOS`` is true, or the supported platforms say ``iPhoneOS``, or a
      bundle without the macOS ``Contents/`` layout declares ``UIDeviceFamily``.
    * ``catalyst``: a macOS-layout bundle whose ``UIDeviceFamily`` includes 6 (the Mac idiom),
      or that carries at least two UIKit-only keys (``UIDeviceFamily``,
      ``UIApplicationSceneManifest``, ``UILaunchStoryboardName``, …). One stray key is not
      enough: SwiftUI multiplatform apps sometimes leak a single UIKit key into their Mac plist.
    """
    if info.get("LSRequiresIPhoneOS") is True or "iphoneos" in _platforms(info):
        return "ios"
    families = _device_families(info)
    if not mac_layout:
        return "ios" if families else None
    if _MAC_IDIOM in families or len(_UIKIT_PLIST_KEYS & set(info)) >= 2:
        return "catalyst"
    return None


def _structure_kind(root: str, probe: _Probe) -> AppKind | None:
    """Kinds that the bundle's layout reveals, most specific first."""
    if probe.exists(posixpath.join(root, "Wrapper")) or probe.exists(posixpath.join(root, "WrappedBundle")):
        return "ios"
    mac_layout = probe.exists(posixpath.join(root, "Contents"))
    info_path = (
        posixpath.join(root, "Contents", "Info.plist") if mac_layout else posixpath.join(root, "Info.plist")
    )
    info = probe.read_plist(info_path)
    if not mac_layout:
        return _uikit_kind(info, mac_layout=False)
    kind = _framework_kind(root, probe) or _java_kind(root, probe, info)
    if kind is not None:
        return kind
    if probe.exists(posixpath.join(root, "Contents", "MacOS", "XUL")):
        return "firefox"  # Gecko-based (Firefox forks, Thunderbird)
    return _uikit_kind(info, mac_layout=True)


def _is_standard_app(root: str, probe: _Probe) -> bool:
    contents = posixpath.join(root, "Contents")
    return probe.exists(posixpath.join(contents, "Info.plist")) or probe.exists(
        posixpath.join(contents, "MacOS")
    )


def detect_app_kind(bundle_id: str | None, bundle_path: str | None, *, fs: BundleFS = LOCAL_FS) -> AppKind:
    """Classify an app from its bundle id and ``.app`` path (see :data:`AppKind`).

    Precedence, most decisive first:

    1. bundle ids that settle it: Safari, Chromium browsers, Firefox (``org.mozilla.*``),
       Microsoft Office, Adobe (``com.adobe.*``);
    2. the bundle layout: iOS wrappers, ``Electron Framework``, ``Chromium Embedded
       Framework``, ``Qt*.framework``/``libqcocoa``, Chromium renderer helpers, Java runtimes
       and launchers, Gecko's ``XUL``, then Info.plist UIKit keys (iOS / Catalyst);
    3. well-known bundle ids (Slack, VS Code, JetBrains IDEs, Apple's Catalyst apps, other
       ``com.apple.*`` apps are native);
    4. ``"native"`` for any other standard app bundle, else ``"unknown"``.

    ``bundle_path`` may point inside the bundle (its executable); ``fs`` is the file system to
    read (:data:`LOCAL_FS` by default). Never raises: unreadable bundles count as missing.
    """
    folded_id = (bundle_id or "").strip().casefold()
    if folded_id:
        decided = _bundle_id_kind(folded_id)
        if decided is not None:
            return decided
    probe = _Probe(fs)
    root = bundle_root(bundle_path)
    present = root is not None and probe.exists(root)
    if present and root is not None:
        structural = _structure_kind(root, probe)
        if structural is not None:
            return structural
    if folded_id and not present:
        # Well-known ids only stand in for a bundle we cannot read; a readable bundle without
        # framework markers is native even if its id usually belongs to an Electron app.
        guessed = _fallback_id_kind(folded_id)
        if guessed is not None:
            return guessed
    if present and root is not None and _is_standard_app(root, probe):
        return "native"
    return "unknown"


# --------------------------------------------------------------------------- AX quality
MIN_USEFUL = 1
"""Fewer named controls/texts than this (i.e. none at all) → ``poor``."""
POOR_FRAMED_RATIO = 0.5
"""Less than half of the nodes have a usable frame → ``poor`` (nothing can be located)."""
POOR_NAMED_RATIO = 0.25
POOR_MIN_UNNAMED = 3
POOR_TEXT_RESCUE = 5
"""Under 25% of the controls named, at least 3 unnamed ones, and fewer than 5 texts → ``poor``."""
POOR_OPAQUE_RATIO = 0.5
POOR_OPAQUE_MAX_USEFUL = 5
"""An unlabeled, childless region covering ≥ 50% of the window (a canvas) with fewer than 5
useful elements → ``poor``."""
GOOD_NAMED_RATIO = 0.8
GOOD_FRAMED_RATIO = 0.9
GOOD_OPAQUE_RATIO = 0.3
"""``good`` needs ≥ 80% of the controls named, ≥ 90% of the nodes framed, no opaque region of
≥ 30% of the window and no empty web area; everything in between is ``partial``."""
WEB_AREA_MIN_CONTENT = 2
"""A web area with fewer content nodes (controls or texts) than this counts as empty."""

_READABLE_ROLES = frozenset({"AXStaticText", "AXHeading"})


@dataclass(frozen=True, slots=True)
class AxMetrics:
    """Measurable facts about a tree, from which :func:`quality_from_metrics` grades it.

    Counts cover visible nodes below the root, excluding the window's traffic-light buttons.
    """

    nodes: int
    interactive: int
    named_interactive: int
    framed: int
    text_nodes: int
    """Static texts and headings that carry text."""
    valued_inputs: int
    """Text fields/areas whose value is readable (non-empty, not secure)."""
    web_areas: int
    empty_web_areas: int
    opaque_ratio: float
    """Largest unlabeled, childless node's share of the window area (0 without a window frame)."""

    @property
    def named_ratio(self) -> float:
        """Share of interactive nodes that have a name (1.0 when there are none)."""
        return self.named_interactive / self.interactive if self.interactive else 1.0

    @property
    def framed_ratio(self) -> float:
        """Share of nodes with a non-empty frame (1.0 for an empty tree)."""
        return self.framed / self.nodes if self.nodes else 1.0

    @property
    def unnamed_interactive(self) -> int:
        return self.interactive - self.named_interactive

    @property
    def useful(self) -> int:
        """Elements an agent can identify: named controls, texts and readable inputs."""
        return self.named_interactive + self.text_nodes + self.valued_inputs

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": self.nodes,
            "interactive": self.interactive,
            "named_interactive": self.named_interactive,
            "framed": self.framed,
            "text_nodes": self.text_nodes,
            "valued_inputs": self.valued_inputs,
            "web_areas": self.web_areas,
            "empty_web_areas": self.empty_web_areas,
            "named_ratio": round(self.named_ratio, 3),
            "framed_ratio": round(self.framed_ratio, 3),
            "opaque_ratio": round(self.opaque_ratio, 3),
        }


def _visible_descendants(root: RawNode) -> Iterator[tuple[RawNode, RawNode | None]]:
    """Visible nodes below ``root`` with their nearest enclosing web area (pre-order)."""
    stack: list[tuple[RawNode, RawNode | None]] = [
        (child, root if root.role == "AXWebArea" else None) for child in reversed(root.children)
    ]
    while stack:
        node, web_area = stack.pop()
        if not node.visible or node.subrole in WINDOW_FRAME_SUBROLES:
            continue
        yield node, web_area
        inner = node if node.role == "AXWebArea" else web_area
        stack.extend((child, inner) for child in reversed(node.children))


def _has_frame(node: RawNode) -> bool:
    return node.frame is not None and node.frame.w > 0 and node.frame.h > 0


def _readable_value(node: RawNode) -> bool:
    return (
        node.role in TEXT_INPUT_ROLES
        and not node.secure
        and node.value is not None
        and bool(str(node.value).strip())
    )


def _window_area(tree: RawTree) -> float:
    if tree.window is not None:
        return tree.window.frame.area
    return tree.root.frame.area if tree.root.frame is not None else 0.0


def measure_ax(tree: RawTree) -> AxMetrics:
    """Count what :func:`assess_ax_quality` needs (one pass over the visible tree)."""
    nodes = interactive = named = framed = texts = valued = 0
    largest_opaque = 0.0
    web_content: dict[int, int] = {}
    for node, web_area in _visible_descendants(tree.root):
        nodes += 1
        name = node_name(node)
        has_frame = _has_frame(node)
        framed += has_frame
        acts = is_interactive(node)
        readable = node.role in _READABLE_ROLES and name is not None
        has_value = _readable_value(node)
        if acts:
            interactive += 1
            named += name is not None
        texts += readable
        valued += has_value
        if node.role == "AXWebArea":
            web_content.setdefault(id(node), 0)
        if web_area is not None and (acts or readable or has_value):
            web_content[id(web_area)] = web_content.get(id(web_area), 0) + 1
        if has_frame and not node.children and name is None and not has_value and node.frame is not None:
            largest_opaque = max(largest_opaque, node.frame.area)
    area = _window_area(tree)
    return AxMetrics(
        nodes=nodes,
        interactive=interactive,
        named_interactive=named,
        framed=framed,
        text_nodes=texts,
        valued_inputs=valued,
        web_areas=len(web_content),
        empty_web_areas=sum(1 for count in web_content.values() if count < WEB_AREA_MIN_CONTENT),
        opaque_ratio=min(largest_opaque / area, 1.0) if area > 0 else 0.0,
    )


def quality_from_metrics(metrics: AxMetrics) -> AxQuality:
    """Grade :class:`AxMetrics` with the documented thresholds (see the module constants)."""
    m = metrics
    if m.nodes == 0 or m.useful < MIN_USEFUL or m.framed_ratio < POOR_FRAMED_RATIO:
        return "poor"
    if m.opaque_ratio >= POOR_OPAQUE_RATIO and m.useful < POOR_OPAQUE_MAX_USEFUL:
        return "poor"
    if (
        m.named_ratio < POOR_NAMED_RATIO
        and m.unnamed_interactive >= POOR_MIN_UNNAMED
        and m.text_nodes < POOR_TEXT_RESCUE
    ):
        return "poor"
    if (
        m.named_ratio >= GOOD_NAMED_RATIO
        and m.framed_ratio >= GOOD_FRAMED_RATIO
        and m.opaque_ratio < GOOD_OPAQUE_RATIO
        and m.empty_web_areas == 0
    ):
        return "good"
    return "partial"


def assess_ax_quality(tree: RawTree) -> AxQuality:
    """``"good"``, ``"partial"`` or ``"poor"``: how well the tree describes the app's UI.

    Based on node count, the share of controls with names, readable texts, frames, empty web
    areas and large unlabeled (custom-drawn) regions; see :func:`quality_from_metrics`."""
    return quality_from_metrics(measure_ax(tree))


def quality_rank(quality: AxQuality) -> int:
    """0 for ``poor``, 1 for ``partial``, 2 for ``good``."""
    return AX_QUALITIES.index(quality)


# --------------------------------------------------------------------------- profiles
_BASE_STRATEGIES: dict[str, tuple[Strategy, ...]] = {
    "native": ("ax_actions",),
    "catalyst": ("ax_actions",),
    "ios": ("ax_actions", "vision_fallback"),
    "electron": ("enhance_ax", "web_area", "ax_actions"),
    "cef": ("enhance_ax", "web_area", "ax_actions"),
    "chromium_browser": ("enhance_ax", "web_area", "browser_toolset", "ax_actions"),
    "safari": ("web_area", "browser_toolset", "ax_actions"),
    "firefox": ("web_area", "ax_actions"),
    "java": ("ax_actions", "menus_first", "vision_fallback"),
    "qt": ("ax_actions", "menus_first", "vision_fallback"),
    "office": ("menus_first", "ax_actions"),
    "adobe": ("menus_first", "ax_actions", "vision_fallback"),
    "unknown": ("ax_actions",),
}

_KIND_NOTES: dict[str, str] = {
    "native": "Native Mac app: the accessibility tree is the main interface; use refs and select_menu.",
    "catalyst": (
        "Mac Catalyst (UIKit) app: controls are in the tree, but swipes and long presses need coordinates."
    ),
    "ios": (
        "iPhone/iPad app running on the Mac: its tree is often thin; expect OCR text (o-refs) and "
        "coordinate clicks."
    ),
    "electron": (
        "Electron app: AXPilot switches on its full accessibility tree; content is inside a web area. "
        "If the first snapshot is sparse, snapshot again after a moment."
    ),
    "cef": (
        "Chromium Embedded Framework app: content is inside a web area once accessibility is on; "
        "snapshot again if the first one is sparse."
    ),
    "chromium_browser": "Chromium browser: page content is in the AXWebArea.",
    "safari": "Safari: page content is in the AXWebArea.",
    "firefox": (
        "Gecko (Firefox family) app: page content is in the AXWebArea; the browser toolset cannot script it."
    ),
    "java": (
        "Java app: the Java accessibility bridge exposes a partial tree. Menus and keyboard shortcuts are "
        "the most reliable; OCR fills gaps."
    ),
    "qt": (
        "Qt app: accessibility varies by Qt version; prefer menus and shortcuts and check names before "
        "clicking."
    ),
    "office": (
        "Microsoft Office: large trees; use select_menu and named ribbon buttons, and scope snapshots with "
        "window_id."
    ),
    "adobe": (
        "Adobe app: panels are custom-drawn; use menus and shortcuts first, OCR for canvases and panels."
    ),
    "unknown": "App kind not recognized; treating it as a standard Mac app.",
}


@dataclass(slots=True)
class AppProfile:
    """How to drive one app: its kind, tree quality, strategies (best first) and notes."""

    kind: AppKind
    ax_quality: AxQuality | None
    """``None`` when no tree was available to assess."""
    strategies: list[str]
    """Strategy names from :data:`STRATEGIES`, in the order to try them."""
    notes: list[str]
    """Short guidance for the model (kind-specific advice, quality warnings)."""
    metrics: AxMetrics | None = field(default=None, repr=False)

    @property
    def label(self) -> str:
        """Human name of the kind (``"Electron app"``)."""
        return KIND_LABELS.get(self.kind, self.kind)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict for ``structuredContent``."""
        return {
            "kind": self.kind,
            "label": self.label,
            "ax_quality": self.ax_quality,
            "strategies": list(self.strategies),
            "notes": list(self.notes),
            "metrics": self.metrics.to_dict() if self.metrics is not None else None,
        }


def _strategies(kind: AppKind, quality: AxQuality | None, bundle_id: str) -> list[str]:
    ordered: list[str] = list(_BASE_STRATEGIES.get(kind, ("ax_actions",)))
    if "browser_toolset" in ordered and bundle_id not in BROWSER_TOOLSET_BUNDLE_IDS:
        ordered.remove("browser_toolset")
    if quality == "partial" and "vision_fallback" not in ordered:
        ordered.append("vision_fallback")
    if quality == "poor":
        rest = [name for name in ordered if name != "vision_fallback"]
        lead = [name for name in rest if name == "enhance_ax"]  # automatic; keep it visible first
        ordered = [*lead, "vision_fallback", *(name for name in rest if name != "enhance_ax")]
    ordered.append("coordinates")
    return ordered


def _kind_note(kind: AppKind, bundle_id: str) -> str:
    note = _KIND_NOTES.get(kind, _KIND_NOTES["unknown"])
    if kind in ("chromium_browser", "safari"):
        if bundle_id in BROWSER_TOOLSET_BUNDLE_IDS:
            note += " browser_tabs / browser_read work when the browser toolset is enabled."
        else:
            note += " The browser toolset does not support this browser; read pages through the web area."
    return note


def _quality_notes(quality: AxQuality | None, metrics: AxMetrics | None, kind: AppKind) -> list[str]:
    if quality is None or metrics is None:
        return ["AX quality was not assessed (no tree captured)."]
    notes: list[str] = []
    if metrics.nodes == 0:
        notes.append("The app shows no window content in the accessibility tree (no window open?).")
    if metrics.empty_web_areas:
        notes.append(
            "A web area has no content yet (accessibility may still be switching on); snapshot again."
        )
    if quality == "poor":
        notes.append(
            f"Accessibility tree is poor ({metrics.nodes} nodes, {metrics.named_interactive} of "
            f'{metrics.interactive} controls named): use snapshot mode="vision" (OCR text as o-refs) or a '
            "screenshot with coordinate clicks."
        )
        if kind in ("electron", "cef", "chromium_browser"):
            notes.append("If the tree stays empty, quit and reopen the app while AXPilot is running.")
    elif quality == "partial":
        notes.append(
            f"Accessibility tree is partial ({metrics.unnamed_interactive} unnamed controls): check names "
            "before acting; OCR (o-refs) can fill gaps."
        )
    return notes


def app_profile(
    app: AppInfo,
    tree: RawTree | None,
    *,
    fs: BundleFS = LOCAL_FS,
    kind: AppKind | None = None,
) -> AppProfile:
    """The compatibility profile of ``app``: kind (detected unless given), AX quality of ``tree``
    (``None`` without a tree), ordered strategies and notes for the model."""
    detected: AppKind = kind if kind is not None else detect_app_kind(app.bundle_id, app.path, fs=fs)
    metrics = measure_ax(tree) if tree is not None else None
    quality = quality_from_metrics(metrics) if metrics is not None else None
    folded_id = (app.bundle_id or "").casefold()
    return AppProfile(
        kind=detected,
        ax_quality=quality,
        strategies=_strategies(detected, quality, folded_id),
        notes=[_kind_note(detected, folded_id), *_quality_notes(quality, metrics, detected)],
        metrics=metrics,
    )

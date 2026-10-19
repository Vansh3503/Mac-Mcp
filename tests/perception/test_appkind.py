"""App-kind detection, AX quality grading and compatibility profiles."""

from __future__ import annotations

import json
import plistlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from axpilot.engine.fake import FakeEngine
from axpilot.models import AppInfo, RawNode, RawTree, Rect, WindowInfo
from axpilot.perception import (
    APP_KINDS,
    STRATEGIES,
    AppProfile,
    BundleFS,
    LocalBundleFS,
    MemoryBundleFS,
    app_profile,
    assess_ax_quality,
    detect_app_kind,
    measure_ax,
)
from axpilot.perception import appkind as ak
from axpilot.perception.serialize import assign_handles
from axpilot.perception.snapshot import node_name
from axpilot.system.browser import BROWSERS

APP = "/Applications/Thing.app"


def bundle(*files: str, plist: dict[str, Any] | None = None, root: str = APP) -> MemoryBundleFS:
    """An in-memory bundle at ``root`` with relative ``files`` and an optional Contents/Info.plist."""
    plists = {f"{root}/Contents/Info.plist": plist} if plist is not None else {}
    return MemoryBundleFS([f"{root}/{item}" for item in files], plists)


def kind_of(fs: BundleFS, bundle_id: str | None = "com.example.thing", path: str | None = APP) -> str:
    return detect_app_kind(bundle_id, path, fs=fs)


STANDARD = ("Contents/MacOS/Thing",)
APPL = {"CFBundlePackageType": "APPL", "CFBundleExecutable": "Thing"}


# =========================================================================== detection: bundle layout
@pytest.mark.parametrize(
    ("files", "plist", "expected"),
    [
        (
            ("Contents/Frameworks/Electron Framework.framework/Versions/A/Electron Framework",),
            APPL,
            "electron",
        ),
        (("Contents/Frameworks/Chromium Embedded Framework.framework/",), APPL, "cef"),
        (("Contents/Frameworks/QtCore.framework/", "Contents/Frameworks/QtGui.framework/"), APPL, "qt"),
        (("Contents/Frameworks/Qt3DCore.framework/",), APPL, "qt"),
        (("Contents/Frameworks/libQt5Core.5.dylib",), APPL, "qt"),
        (("Contents/PlugIns/platforms/libqcocoa.dylib",), APPL, "qt"),
        (("Contents/Frameworks/QTKit.framework/",), APPL, "native"),  # Apple's QuickTime kit, not Qt
        (("Contents/PlugIns/jdk-17.0.2.jdk/Contents/Home/",), APPL, "java"),
        (("Contents/PlugIns/JRE.bundle/",), APPL, "java"),
        (("Contents/runtime/Contents/Home/lib/libjli.dylib",), APPL, "java"),
        (("Contents/jbr/Contents/Home/bin/java",), APPL, "java"),
        (("Contents/Java/app.jar",), APPL, "java"),
        (("Contents/Resources/Java/app.jar",), APPL, "java"),
        (("Contents/MacOS/JavaAppLauncher",), APPL, "java"),
        (("Contents/MacOS/universalJavaApplicationStub",), APPL, "java"),
        (STANDARD, {**APPL, "CFBundleExecutable": "JavaApplicationStub"}, "java"),
        (STANDARD, {**APPL, "JVMOptions": {"MainClass": "Main"}}, "java"),
        (("Contents/runtime/README",), APPL, "native"),  # a folder called runtime is not enough
        (("Wrapper/Thing.app/Info.plist",), None, "ios"),
        (("WrappedBundle",), None, "ios"),
        (STANDARD, {**APPL, "LSRequiresIPhoneOS": True}, "ios"),
        (STANDARD, {**APPL, "CFBundleSupportedPlatforms": ["iPhoneOS"]}, "ios"),
        (STANDARD, {**APPL, "UIDeviceFamily": [2, 6]}, "catalyst"),
        (STANDARD, {**APPL, "UIDeviceFamily": 6}, "catalyst"),
        (
            STANDARD,
            {**APPL, "UIApplicationSceneManifest": {}, "UILaunchStoryboardName": "Launch"},
            "catalyst",
        ),
        (STANDARD, {**APPL, "UIApplicationSceneManifest": {}}, "native"),  # one stray key is not enough
        (STANDARD, {**APPL, "UIDeviceFamily": ["x", None]}, "native"),
        (
            ("Contents/Frameworks/Thorium Framework.framework/Helpers/Thorium Helper (Renderer).app/",),
            APPL,
            "chromium_browser",
        ),
        (
            (
                "Contents/Frameworks/Thorium Framework.framework/Versions/1.0/Helpers/"
                "Thorium Helper (Renderer).app/",
            ),
            APPL,
            "chromium_browser",
        ),
        (("Contents/Frameworks/Thing Framework.framework/Helpers/Thing Helper.app/",), APPL, "native"),
        (("Contents/MacOS/XUL",), APPL, "firefox"),
        (STANDARD, APPL, "native"),
        (STANDARD, None, "native"),
        (("Contents/Resources/icon.icns",), None, "unknown"),  # Contents/ without MacOS/ or Info.plist
    ],
)
def test_bundle_layout_decides_the_kind(
    files: tuple[str, ...], plist: dict[str, Any] | None, expected: str
) -> None:
    assert kind_of(bundle(*files, plist=plist)) == expected


def test_contents_folder_without_info_or_executable_is_unknown_when_nothing_else_is_known() -> None:
    fs = MemoryBundleFS([f"{APP}/Contents/"])
    assert kind_of(fs) == "unknown"
    assert kind_of(MemoryBundleFS([f"{APP}/Contents/Info.plist"])) == "native"


def test_a_bare_ios_bundle_is_detected_from_its_root_info_plist() -> None:
    root = "/Applications/Game.app/Wrapper/Game.app"
    fs = MemoryBundleFS([], {f"{root}/Info.plist": {"UIDeviceFamily": [1, 2]}})
    assert detect_app_kind("com.example.game", root, fs=fs) == "ios"
    plain = MemoryBundleFS([f"{root}/Game"], {f"{root}/Info.plist": {"CFBundleName": "Game"}})
    assert detect_app_kind("com.example.game", root, fs=plain) == "unknown"


def test_bundle_path_inside_the_bundle_resolves_to_the_app() -> None:
    fs = bundle("Contents/Frameworks/Electron Framework.framework/", plist=APPL)
    assert kind_of(fs, path=f"{APP}/Contents/MacOS/Thing") == "electron"
    assert kind_of(fs, path=f"{APP}/") == "electron"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/Applications/Foo.app", "/Applications/Foo.app"),
        ("/Applications/Foo.app/", "/Applications/Foo.app"),
        ("/Applications/Foo.app/Contents/MacOS/Foo", "/Applications/Foo.app"),
        (
            "/Applications/Xcode.app/Contents/Developer/Applications/Simulator.app",
            "/Applications/Xcode.app/Contents/Developer/Applications/Simulator.app",
        ),
        (
            "/Applications/Xcode.app/Contents/Developer/Applications/Simulator.app/Contents/MacOS/Simulator",
            "/Applications/Xcode.app/Contents/Developer/Applications/Simulator.app",
        ),
        ("/Applications/X.APP/Contents/MacOS/x", "/Applications/X.APP"),
        ("/usr/bin/python3", "/usr/bin/python3"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_bundle_root(path: str | None, expected: str | None) -> None:
    assert ak.bundle_root(path) == expected


# =========================================================================== detection: bundle ids
@pytest.mark.parametrize(
    ("bundle_id", "expected"),
    [
        ("com.apple.Safari", "safari"),
        ("COM.APPLE.SAFARI", "safari"),
        ("com.apple.SafariTechnologyPreview", "safari"),
        ("com.google.Chrome", "chromium_browser"),
        ("com.google.Chrome.canary", "chromium_browser"),
        ("company.thebrowser.Browser", "chromium_browser"),
        ("com.microsoft.edgemac.Beta", "chromium_browser"),
        ("com.brave.Browser.nightly", "chromium_browser"),
        ("org.chromium.Chromium", "chromium_browser"),
        ("com.vivaldi.Vivaldi", "chromium_browser"),
        ("com.operasoftware.OperaGX", "chromium_browser"),
        ("org.mozilla.firefox", "firefox"),
        ("org.mozilla.nightly", "firefox"),
        ("com.microsoft.Word", "office"),
        ("com.microsoft.Excel", "office"),
        ("com.microsoft.Powerpoint", "office"),
        ("com.microsoft.Outlook", "office"),
        ("com.microsoft.onenote.mac", "office"),
        ("com.adobe.Photoshop", "adobe"),
        ("com.adobe.Acrobat.Pro", "adobe"),
        ("com.microsoft.VSCode", "electron"),
        ("com.tinyspeck.slackmacgap", "electron"),
        ("com.spotify.client", "cef"),
        ("com.jetbrains.pycharm", "java"),
        ("com.google.android.studio", "java"),
        ("com.apple.news", "catalyst"),
        ("com.apple.MobileSMS", "catalyst"),
        ("com.apple.Notes", "native"),
        ("com.example.unknown", "unknown"),
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_bundle_ids_decide_when_the_bundle_cannot_be_read(bundle_id: str | None, expected: str) -> None:
    assert detect_app_kind(bundle_id, None, fs=MemoryBundleFS()) == expected
    assert detect_app_kind(bundle_id, "/Applications/Missing.app", fs=MemoryBundleFS()) == expected


def test_browser_office_and_adobe_ids_win_over_embedded_frameworks() -> None:
    cef = bundle("Contents/Frameworks/Chromium Embedded Framework.framework/", plist=APPL)
    assert kind_of(cef, "com.microsoft.Word") == "office"
    assert kind_of(cef, "com.adobe.Photoshop") == "adobe"
    assert kind_of(cef, "com.google.Chrome") == "chromium_browser"


def test_the_bundle_wins_over_well_known_ids() -> None:
    native = bundle(*STANDARD, plist=APPL)
    assert kind_of(native, "com.microsoft.VSCode") == "native"  # its bundle has no Electron framework
    catalyst = bundle(*STANDARD, plist={**APPL, "UIDeviceFamily": [2, 6]})
    assert kind_of(catalyst, "com.apple.Notes") == "catalyst"


def test_every_detected_kind_is_a_declared_app_kind() -> None:
    assert set(ak.KIND_LABELS) == set(APP_KINDS)
    assert set(ak._BASE_STRATEGIES) == set(APP_KINDS)


def test_browser_toolset_ids_mirror_the_browser_module() -> None:
    assert {spec.bundle_id.casefold() for spec in BROWSERS.values()} == ak.BROWSER_TOOLSET_BUNDLE_IDS
    for bundle_id in ak.BROWSER_TOOLSET_BUNDLE_IDS:
        assert detect_app_kind(bundle_id, None, fs=MemoryBundleFS()) in ("safari", "chromium_browser")


# =========================================================================== detection: robustness
class FaultyFS:
    """A file system whose every call fails (permission errors, bugs in an injected fs…)."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def exists(self, path: str) -> bool:
        self.calls += 1
        raise self.error

    def listdir(self, path: str) -> list[str]:
        self.calls += 1
        raise self.error

    def read_plist(self, path: str) -> dict[str, Any] | None:
        self.calls += 1
        raise self.error


@pytest.mark.parametrize("error", [OSError(13, "Permission denied"), RuntimeError("boom"), ValueError("bad")])
def test_a_failing_file_system_degrades_to_bundle_id_rules(error: Exception) -> None:
    fs = FaultyFS(error)
    assert detect_app_kind("com.example.thing", APP, fs=fs) == "unknown"
    assert detect_app_kind("com.apple.Notes", APP, fs=fs) == "native"
    assert detect_app_kind("com.google.Chrome", APP, fs=fs) == "chromium_browser"
    assert fs.calls >= 1


class PartlyBrokenFS(MemoryBundleFS):
    """Lists fine but returns junk from read_plist/listdir for some paths."""

    def read_plist(self, path: str) -> Any:
        return ["not", "a", "dict"]

    def listdir(self, path: str) -> Any:
        return None if path.endswith("Frameworks") else super().listdir(path)


def test_junk_plists_and_listings_count_as_missing() -> None:
    fs = PartlyBrokenFS([f"{APP}/Contents/MacOS/Thing", f"{APP}/Contents/Info.plist"])
    assert kind_of(fs) == "native"


def test_the_bundle_fs_protocol_is_runtime_checkable() -> None:
    assert isinstance(LocalBundleFS(), BundleFS)
    assert isinstance(MemoryBundleFS(), BundleFS)
    assert isinstance(ak.LOCAL_FS, LocalBundleFS)


# =========================================================================== file systems
def test_memory_fs_paths_parents_and_copies() -> None:
    fs = MemoryBundleFS(["/A.app/Contents/MacOS/a", "B.app//x/"], {"/A.app/Contents/Info.plist": {"k": 1}})
    assert fs.exists("/A.app")
    assert fs.exists("/A.app/Contents/")
    assert fs.exists("/B.app/x")
    assert fs.exists("/A.app/Contents/../Contents/MacOS")
    assert not fs.exists("/A.app/Contents/Frameworks")
    assert fs.listdir("/A.app/Contents") == ["Info.plist", "MacOS"]
    assert fs.listdir("/") == ["A.app", "B.app"]
    assert fs.listdir("/A.app/Contents/MacOS/a") == []
    plist = fs.read_plist("/A.app/Contents/Info.plist")
    assert plist == {"k": 1}
    assert plist is not None
    plist["k"] = 2
    assert fs.read_plist("/A.app/Contents/Info.plist") == {"k": 1}
    assert fs.read_plist("/A.app/Contents/MacOS/a") is None


def make_bundle(
    tmp_path: Path, *files: str, plist: dict[str, Any] | None = None, binary: bool = False
) -> Path:
    root = tmp_path / "Thing.app"
    for item in files:
        target = root / item
        if item.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"")
    if plist is not None:
        info = root / "Contents" / "Info.plist"
        info.parent.mkdir(parents=True, exist_ok=True)
        fmt = plistlib.FMT_BINARY if binary else plistlib.FMT_XML
        info.write_bytes(plistlib.dumps(plist, fmt=fmt))
    return root


def test_local_fs_detects_real_bundles(tmp_path: Path) -> None:
    electron = make_bundle(
        tmp_path, "Contents/Frameworks/Electron Framework.framework/", "Contents/MacOS/Thing", plist=APPL
    )
    assert detect_app_kind("com.example.thing", str(electron)) == "electron"
    assert detect_app_kind("com.example.thing", str(electron / "Contents" / "MacOS" / "Thing")) == "electron"


def test_local_fs_reads_xml_and_binary_plists(tmp_path: Path) -> None:
    root = make_bundle(
        tmp_path, "Contents/MacOS/Thing", plist={**APPL, "UIDeviceFamily": [2, 6]}, binary=True
    )
    fs = LocalBundleFS()
    assert fs.read_plist(str(root / "Contents" / "Info.plist")) == {**APPL, "UIDeviceFamily": [2, 6]}
    assert detect_app_kind("com.example.thing", str(root), fs=fs) == "catalyst"
    assert fs.listdir(str(root / "Contents")) == ["Info.plist", "MacOS"]
    assert fs.listdir(str(root / "Contents" / "Info.plist")) == []
    assert fs.listdir(str(tmp_path / "missing")) == []
    assert fs.exists(str(root))
    assert not fs.exists(str(tmp_path / "missing"))


@pytest.mark.parametrize("content", [b"<?xml garbage", b"\x00\x01binary junk", b"", "not a dict"])
def test_local_fs_ignores_corrupt_plists(tmp_path: Path, content: bytes | str) -> None:
    path = tmp_path / "Info.plist"
    path.write_bytes(plistlib.dumps(["list"]) if isinstance(content, str) else content)
    assert LocalBundleFS().read_plist(str(path)) is None
    assert LocalBundleFS().read_plist(str(tmp_path / "missing.plist")) is None
    assert LocalBundleFS().read_plist(str(tmp_path)) is None  # a directory


def test_local_fs_refuses_oversized_plists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "Info.plist"
    path.write_bytes(plistlib.dumps({"big": "x" * 200}))
    monkeypatch.setattr(ak, "_PLIST_MAX_BYTES", 64)
    assert LocalBundleFS().read_plist(str(path)) is None


# =========================================================================== AX quality
def node(
    role: str,
    *children: RawNode,
    frame: tuple[float, float, float, float] | None = (10, 10, 40, 20),
    **kw: Any,
) -> RawNode:
    return RawNode(role=role, frame=Rect(*frame) if frame else None, children=list(children), **kw)


def tree_of(
    *children: RawNode, window: bool = True, frame: tuple[float, float, float, float] = (0, 0, 800, 600)
) -> RawTree:
    app = AppInfo(name="Demo", pid=42, bundle_id="com.example.demo")
    if not window:
        root = RawNode(role="AXApplication", title="Demo", children=list(children))
        return RawTree(app=app, window=None, root=root)
    root = RawNode(role="AXWindow", frame=Rect(*frame), children=list(children), actions=("AXRaise",))
    assign_handles(root)
    info = WindowInfo(window_id=1, pid=42, app="Demo", title="Doc", frame=Rect(*frame))
    return RawTree(app=app, window=info, root=root)


def traffic_lights() -> list[RawNode]:
    return [
        node("AXButton", subrole=sub, actions=("AXPress",), frame=(8, 6, 14, 16))
        for sub in ("AXCloseButton", "AXMinimizeButton", "AXZoomButton")
    ]


def button(name: str | None = "OK", **kw: Any) -> RawNode:
    return node("AXButton", title=name, actions=("AXPress",), **kw)


def text(value: str, **kw: Any) -> RawNode:
    return node("AXStaticText", value=value, **kw)


def test_well_labelled_trees_are_good() -> None:
    assert (
        assess_ax_quality(tree_of(*traffic_lights(), button("Save"), button("Cancel"), text("Ready")))
        == "good"
    )
    terminal = tree_of(
        node(
            "AXScrollArea",
            node("AXTextArea", description="shell", value="$ ls\nfile.txt", frame=(0, 0, 780, 600)),
        )
    )
    assert assess_ax_quality(terminal) == "good"


def test_empty_windows_and_apps_without_windows_are_poor() -> None:
    assert assess_ax_quality(tree_of()) == "poor"
    assert assess_ax_quality(tree_of(*traffic_lights())) == "poor"  # traffic lights do not count
    assert assess_ax_quality(tree_of(window=False)) == "poor"
    hidden = tree_of(node("AXGroup", button("Hidden"), visible=False))
    assert assess_ax_quality(hidden) == "poor"


def test_a_canvas_with_little_else_is_poor() -> None:
    canvas = tree_of(button("Export"), button(None), node("AXGroup", frame=(0, 50, 800, 550)))
    metrics = measure_ax(canvas)
    assert metrics.opaque_ratio > 0.9
    assert assess_ax_quality(canvas) == "poor"
    labelled = tree_of(
        button("Export"),
        button("Undo"),
        *[text(f"t{i}") for i in range(4)],
        node("AXGroup", frame=(0, 50, 800, 550)),
    )
    assert assess_ax_quality(labelled) == "partial"  # enough to work with, but the canvas is opaque


def test_mostly_unnamed_controls_are_poor_unless_text_rescues_them() -> None:
    icons = [button(None) for _ in range(4)]
    assert assess_ax_quality(tree_of(*icons, text("Title"))) == "poor"
    assert assess_ax_quality(tree_of(*icons, *[text(f"line {i}") for i in range(5)])) == "partial"
    assert assess_ax_quality(tree_of(button(None), button(None), text("Title"))) == "partial"  # < 3 unnamed


def test_named_ratio_threshold_for_good() -> None:
    four_of_five = tree_of(*[button(f"b{i}") for i in range(4)], button(None))
    assert measure_ax(four_of_five).named_ratio == 0.8
    assert assess_ax_quality(four_of_five) == "good"
    three_of_four = tree_of(*[button(f"b{i}") for i in range(3)], button(None))
    assert assess_ax_quality(three_of_four) == "partial"


def test_missing_frames_make_a_tree_poor_or_partial() -> None:
    frameless = tree_of(*[button(f"b{i}", frame=None) for i in range(3)], button("framed"))
    assert measure_ax(frameless).framed_ratio == 0.25
    assert assess_ax_quality(frameless) == "poor"
    some = tree_of(*[button(f"b{i}") for i in range(9)], button("loose", frame=None))
    assert assess_ax_quality(some) == "good"
    more = tree_of(*[button(f"b{i}") for i in range(8)], button("x", frame=None), button("y", frame=None))
    assert assess_ax_quality(more) == "partial"


def test_web_areas_need_content() -> None:
    empty = tree_of(
        button("Back"), button("Reload"), node("AXWebArea", description="Page", frame=(0, 50, 800, 500))
    )
    metrics = measure_ax(empty)
    assert (metrics.web_areas, metrics.empty_web_areas) == (1, 1)
    assert assess_ax_quality(empty) == "partial"
    full = tree_of(
        button("Back"),
        node(
            "AXWebArea", text("Hello"), node("AXLink", text("Docs"), actions=("AXPress",)), description="Page"
        ),
    )
    assert measure_ax(full).empty_web_areas == 0
    assert assess_ax_quality(full) == "good"
    electron_unenhanced = tree_of(node("AXGroup", node("AXWebArea", frame=(0, 0, 800, 600))))
    assert assess_ax_quality(electron_unenhanced) == "poor"


def test_secure_values_are_not_readable_content() -> None:
    secure = tree_of(node("AXTextField", subrole="AXSecureTextField", secure=True, value="hunter2"))
    assert measure_ax(secure).valued_inputs == 0
    assert assess_ax_quality(secure) == "poor"


def test_metrics_payload_and_ranks() -> None:
    payload = measure_ax(tree_of(button("A"), text("B"))).to_dict()
    assert payload["nodes"] == 2
    assert payload["named_ratio"] == 1.0
    assert set(payload) >= {"opaque_ratio", "empty_web_areas", "framed_ratio"}
    assert [ak.quality_rank(q) for q in ("poor", "partial", "good")] == [0, 1, 2]
    assert measure_ax(tree_of()).named_ratio == 1.0
    assert measure_ax(tree_of()).framed_ratio == 1.0


def test_a_window_without_frames_has_no_opaque_ratio() -> None:
    tree = tree_of(button("A"))
    tree.window = None
    tree.root.frame = None
    assert measure_ax(tree).opaque_ratio == 0.0


def test_demo_desktop_qualities(engine: FakeEngine) -> None:
    for name in ("Slack", "Canvas Paint", "IntelliJ IDEA", "Mail"):
        engine.app_control("launch", name)
    expected = {
        "Finder": "good",
        "TextEdit": "good",
        "Notes": "good",
        "Terminal": "good",
        "System Settings": "good",
        "Slack": "good",
        "Mail": "good",
        "IntelliJ IDEA": "partial",
        "Canvas Paint": "poor",
    }
    for name, quality in expected.items():
        assert assess_ax_quality(engine.capture_tree(app=name)) == quality, name


# --------------------------------------------------------------------------- monotonicity (hypothesis)
ROLES = ("AXButton", "AXCheckBox", "AXStaticText", "AXGroup", "AXImage", "AXTextField", "AXWebArea", "AXRow")


@st.composite
def raw_nodes(draw: st.DrawFn, depth: int = 0) -> RawNode:
    role = draw(st.sampled_from(ROLES))
    named = draw(st.booleans())
    framed = draw(st.integers(0, 4)) > 0
    x, y = draw(st.integers(0, 700)), draw(st.integers(0, 500))
    w, h = draw(st.integers(1, 400)), draw(st.integers(1, 300))
    children: list[RawNode] = []
    if depth < 2 and role in ("AXGroup", "AXWebArea", "AXRow"):
        children = draw(st.lists(raw_nodes(depth + 1), max_size=4))
    return RawNode(
        role=role,
        title="label" if named and role != "AXStaticText" else None,
        value="text" if named and role == "AXStaticText" else None,
        actions=("AXPress",) if role in ("AXButton", "AXCheckBox") else (),
        frame=Rect(x, y, w, h) if framed else None,
        children=children,
    )


raw_trees = st.lists(raw_nodes(), max_size=8).map(lambda children: tree_of(*children))


def _named_everything(tree: RawTree) -> RawTree:
    for item in tree.root.walk():
        if item is not tree.root and node_name(item) is None:
            item.title = "named"
    return tree


def _framed_named(tree: RawTree) -> RawTree:
    for item in tree.root.walk():
        if item is not tree.root and item.frame is None and node_name(item) is not None:
            item.frame = Rect(5, 5, 10, 10)
    return tree


def _with_button(tree: RawTree) -> RawTree:
    tree.root.children.append(button("Added", frame=(20, 20, 30, 20)))
    return tree


@pytest.mark.parametrize("change", [_named_everything, _framed_named, _with_button])
@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(data=st.data())
def test_quality_never_drops_when_the_tree_gets_better(
    change: Callable[[RawTree], RawTree], data: st.DataObject
) -> None:
    children = data.draw(st.lists(raw_nodes(), max_size=8))
    before = assess_ax_quality(tree_of(*children))
    after = assess_ax_quality(change(tree_of(*children)))
    assert ak.quality_rank(after) >= ak.quality_rank(before), (before, after)


@settings(max_examples=100, deadline=None)
@given(tree=raw_trees)
def test_quality_is_always_a_known_grade(tree: RawTree) -> None:
    assert assess_ax_quality(tree) in ak.AX_QUALITIES


# =========================================================================== profiles
def demo_profile(engine: FakeEngine, name: str) -> AppProfile:
    if name not in [app.name for app in engine.list_apps()]:
        engine.app_control("launch", name)
    app = engine.find_app(name)
    return app_profile(app, engine.capture_tree(app=name), fs=engine.bundle_fs())


def test_native_profile(engine: FakeEngine) -> None:
    profile = demo_profile(engine, "Notes")
    assert (profile.kind, profile.ax_quality) == ("native", "good")
    assert profile.strategies == ["ax_actions", "coordinates"]
    assert profile.label == "native AppKit/SwiftUI app"
    assert profile.notes[0].startswith("Native Mac app")


def test_electron_profile(engine: FakeEngine) -> None:
    profile = demo_profile(engine, "Slack")
    assert (profile.kind, profile.ax_quality) == ("electron", "good")
    assert profile.strategies == ["enhance_ax", "web_area", "ax_actions", "coordinates"]
    assert "Electron" in profile.notes[0]


def test_poor_profile_puts_vision_first(engine: FakeEngine) -> None:
    profile = demo_profile(engine, "Canvas Paint")
    assert (profile.kind, profile.ax_quality) == ("native", "poor")
    assert profile.strategies == ["vision_fallback", "ax_actions", "coordinates"]
    assert any('mode="vision"' in note for note in profile.notes)


def test_partial_java_profile(engine: FakeEngine) -> None:
    profile = demo_profile(engine, "IntelliJ IDEA")
    assert (profile.kind, profile.ax_quality) == ("java", "partial")
    assert profile.strategies == ["ax_actions", "menus_first", "vision_fallback", "coordinates"]
    assert any("unnamed controls" in note for note in profile.notes)


def test_browser_profiles_offer_the_toolset_only_when_supported(engine: FakeEngine) -> None:
    safari = demo_profile(engine, "Safari")
    assert safari.kind == "safari"
    assert "browser_toolset" in safari.strategies
    assert "browser_tabs" in safari.notes[0]
    opera = app_profile(AppInfo("Opera", 9, "com.operasoftware.Opera"), None, fs=MemoryBundleFS())
    assert opera.kind == "chromium_browser"
    assert "browser_toolset" not in opera.strategies
    assert "does not support this browser" in opera.notes[0]


def test_partial_profile_adds_vision_and_poor_electron_keeps_enhance_first() -> None:
    app = AppInfo("Thing", 5, "com.tinyspeck.slackmacgap")
    poor = app_profile(
        app, tree_of(node("AXGroup", node("AXWebArea", frame=(0, 0, 800, 600)))), fs=MemoryBundleFS()
    )
    assert poor.strategies == ["enhance_ax", "vision_fallback", "web_area", "ax_actions", "coordinates"]
    assert any("reopen" in note for note in poor.notes)
    partial = app_profile(
        AppInfo("Thing", 5, "com.example.thing"),
        tree_of(button("Back"), node("AXWebArea", description="Page", frame=(0, 50, 800, 500))),
        fs=MemoryBundleFS(),
    )
    assert partial.ax_quality == "partial"
    assert partial.strategies == ["ax_actions", "vision_fallback", "coordinates"]
    assert any("web area has no content" in note for note in partial.notes)


def test_profile_without_tree_and_with_kind_override() -> None:
    profile = app_profile(AppInfo("Word", 7, "com.example.word"), None, kind="office")
    assert profile.kind == "office"
    assert profile.ax_quality is None
    assert profile.metrics is None
    assert profile.strategies == ["menus_first", "ax_actions", "coordinates"]
    assert "not assessed" in profile.notes[-1]
    payload = profile.to_dict()
    assert payload["metrics"] is None
    assert payload["label"] == "Microsoft Office app"


def test_profile_of_an_app_without_windows_mentions_it() -> None:
    profile = app_profile(
        AppInfo("Daemon", 3, "com.example.daemon"), tree_of(window=False), fs=MemoryBundleFS()
    )
    assert profile.ax_quality == "poor"
    assert any("no window" in note for note in profile.notes)


@pytest.mark.parametrize("kind", APP_KINDS)
@pytest.mark.parametrize("quality", ["good", "partial", "poor", None])
def test_strategies_are_known_unique_and_end_with_coordinates(kind: Any, quality: Any) -> None:
    strategies = ak._strategies(kind, quality, "com.apple.safari")
    assert set(strategies) <= set(STRATEGIES)
    assert len(strategies) == len(set(strategies))
    assert strategies[-1] == "coordinates"
    if quality == "poor":
        assert "vision_fallback" in strategies[:2]


def test_profile_payload_is_json_safe(engine: FakeEngine) -> None:
    payload = demo_profile(engine, "Canvas Paint").to_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert payload["metrics"]["opaque_ratio"] > 0.5

from __future__ import annotations

import re
import threading
import time

import pytest

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import AppInfo, Capture, OcrLine, RawNode, RawTree, Rect, WindowInfo
from axpilot.perception import (
    CaptureStore,
    Snapshot,
    SnapshotStore,
    build_snapshot,
    carry_refs,
    describe_refs,
    merge_vision,
    ocr_elements,
    parse_ref,
)
from axpilot.perception.refs import RefCounter
from axpilot.perception.serialize import assign_handles
from axpilot.perception.store import IdFactory

SNAPSHOT_ID = re.compile(r"^s_[0-9a-z]{4}$")
CAPTURE_ID = re.compile(r"^c_[0-9a-z]{4}$")


def make_snapshot(store: SnapshotStore, *, pid: int = 1, buttons: int = 3) -> Snapshot:
    root = RawNode(
        role="AXWindow",
        frame=Rect(0, 0, 400, 400),
        children=[
            RawNode(role="AXButton", title=f"B{i}", frame=Rect(0, i * 20, 40, 20)) for i in range(buttons)
        ],
    )
    assign_handles(root)
    snap = build_snapshot(RawTree(AppInfo(f"App{pid}", pid), None, root), snapshot_id=store.new_id())
    store.put(snap)
    return snap


def make_capture(width: int = 10) -> Capture:
    return Capture(data=b"\x89PNG", format="png", width=width, height=10, region=Rect(0, 0, width, 10))


def expect(code: ErrorCode, func, *args, **kwargs) -> AXPilotError:  # type: ignore[no-untyped-def]
    with pytest.raises(AXPilotError) as info:
        func(*args, **kwargs)
    assert info.value.code is code
    return info.value


# --------------------------------------------------------------------------- ids
def test_ids_have_the_documented_shape_and_are_unique() -> None:
    store = SnapshotStore()
    ids = [store.new_id() for _ in range(5000)]
    assert all(SNAPSHOT_ID.match(i) for i in ids)
    assert len(set(ids)) == len(ids)


def test_ids_are_not_simply_sequential_and_differ_between_processes() -> None:
    first, second = IdFactory("s_"), IdFactory("s_")
    run_a = [first.next() for _ in range(20)]
    run_b = [second.next() for _ in range(20)]
    assert run_a != run_b  # independent random salts and strides
    assert sorted(run_a) != run_a  # each step jumps a third of the id space or more, so it wraps


def test_id_factory_skips_ids_still_in_use() -> None:
    factory = IdFactory("x_")
    probe = IdFactory("x_")
    probe._salt, probe._stride = factory._salt, factory._stride  # same sequence
    blocked = probe.next()
    assert factory.next(taken=lambda ident: ident == blocked) != blocked


# --------------------------------------------------------------------------- snapshot store
def test_put_and_get() -> None:
    store = SnapshotStore()
    snap = make_snapshot(store)
    assert store.get(snap.snapshot_id) is snap
    assert snap.snapshot_id in store
    assert len(store) == 1


def test_missing_snapshot_raises() -> None:
    error = expect(ErrorCode.SNAPSHOT_NOT_FOUND, SnapshotStore().get, "s_zzzz")
    assert "s_zzzz" in error.message


def test_lru_eviction_and_recency() -> None:
    store = SnapshotStore(capacity=2)
    a, b = make_snapshot(store), make_snapshot(store)
    store.get(a.snapshot_id)  # a is now the most recently used
    c = make_snapshot(store)
    assert a.snapshot_id in store
    assert c.snapshot_id in store
    assert b.snapshot_id not in store
    assert len(store) == 2


def test_latest_is_the_most_recently_created_optionally_per_app() -> None:
    store = SnapshotStore()
    assert store.latest() is None
    one = make_snapshot(store, pid=1)
    two = make_snapshot(store, pid=2)
    store.get(one.snapshot_id)  # access does not change what is "latest"
    assert store.latest() is two
    assert store.latest(pid=1) is one
    assert store.latest(pid=3) is None


def test_resolve_against_the_latest_snapshot() -> None:
    store = SnapshotStore()
    make_snapshot(store)
    latest = make_snapshot(store, buttons=5)
    snap, el = store.resolve("e5")
    assert snap is latest
    assert el.name == "B4"


def test_resolve_with_explicit_snapshot_ids() -> None:
    store = SnapshotStore()
    older = make_snapshot(store)
    make_snapshot(store)
    assert store.resolve("e2", older.snapshot_id)[0] is older
    assert store.resolve(f"{older.snapshot_id}:e2")[0] is older
    assert store.resolve(f" {older.snapshot_id.upper()}:E2 ")[1].name == "B1"


def test_resolve_errors() -> None:
    store = SnapshotStore()
    expect(ErrorCode.SNAPSHOT_NOT_FOUND, store.resolve, "e1")
    snap = make_snapshot(store)
    other = make_snapshot(store)
    expect(ErrorCode.INVALID_ARGUMENT, store.resolve, f"{snap.snapshot_id}:e1", other.snapshot_id)
    expect(ErrorCode.INVALID_ARGUMENT, store.resolve, "button 3")
    error = expect(ErrorCode.STALE_REF, store.resolve, "e99")
    assert "never issued" in error.message
    assert "e3" in error.message
    assert error.hint is not None
    assert "snapshot" in error.hint
    error = expect(ErrorCode.STALE_REF, store.resolve, f"{snap.snapshot_id}:e99")
    assert "(it lists e1–e3)" in error.message
    expect(ErrorCode.SNAPSHOT_NOT_FOUND, store.resolve, "s_0000:e1")


@pytest.mark.parametrize(
    ("ref", "expected"),
    [("e1", (None, "e1")), ("E12", (None, "e12")), ("s_ab12:e3", ("s_ab12", "e3")), (" e7 ", (None, "e7"))],
)
def test_parse_ref_accepts_plain_and_qualified_refs(ref: str, expected: tuple[str | None, str]) -> None:
    assert parse_ref(ref) == expected


@pytest.mark.parametrize("ref", ["", "e0", "e", "12", "s_ab12:", "s_ab:e1", "e1:s_ab12", "ref=e1"])
def test_parse_ref_rejects_malformed_refs(ref: str) -> None:
    expect(ErrorCode.INVALID_ARGUMENT, parse_ref, ref)


# --------------------------------------------------------------------------- session refs
def button(title: str, x: float, handle: str) -> RawNode:
    return RawNode(
        role="AXButton", title=title, frame=Rect(x, 10, 60, 20), actions=("AXPress",), handle=handle
    )


def session_snapshot(
    store: SnapshotStore,
    *buttons: RawNode,
    pid: int = 1,
    window_id: int | None = 7,
    previous: Snapshot | None = None,
    put: bool = True,
) -> Snapshot:
    """Build, carry (like ServerContext.build_snapshot) and store a snapshot."""
    root = RawNode(
        role="AXWindow", frame=Rect(0, 0, 800, 600), children=list(buttons), handle=f"w{window_id}"
    )
    app = AppInfo(f"App{pid}", pid, f"com.example.app{pid}")
    window = (
        WindowInfo(window_id, pid, app.name, "Doc", Rect(0, 0, 800, 600)) if window_id is not None else None
    )
    raw = build_snapshot(RawTree(app, window, root), snapshot_id=store.new_id())
    snap = carry_refs(raw, previous, store.refs, history=store.snapshots_of(pid, window_id))
    if put:
        store.put(snap)
    return snap


def ref_of(snap: Snapshot, name: str) -> str:
    return next(el.ref for el in snap.all_elements if el.name == name)


def test_bare_refs_fall_back_to_older_snapshots_newest_first() -> None:
    store = SnapshotStore()
    notes = session_snapshot(store, button("New Note", 0, "new"), button("Share", 70, "share"), pid=1)
    mail = session_snapshot(store, button("Reply", 0, "reply"), pid=2)
    assert store.latest() is mail
    snap, el = store.resolve(ref_of(notes, "Share"))
    assert (snap, el.name) == (notes, "Share")  # a Notes ref still means the Notes element
    assert store.resolve(ref_of(mail, "Reply"))[0] is mail
    newer_notes = session_snapshot(store, button("New Note", 0, "new"), pid=1)
    snap, el = store.resolve(ref_of(notes, "New Note"))
    assert snap is newer_notes  # the newest snapshot listing the ref wins
    assert store.resolve(ref_of(notes, "Share"))[0] is notes  # only the older one lists Share


def test_plain_local_refs_never_fall_back_to_older_snapshots() -> None:
    store = SnapshotStore()
    older = make_snapshot(store, buttons=5)  # plain build_snapshot: e1…e5 are positions, not names
    make_snapshot(store, buttons=2)
    error = expect(ErrorCode.STALE_REF, store.resolve, "e5")
    assert "not listed in any recent snapshot" in error.message
    assert store.resolve("e5", older.snapshot_id)[1].name == "B4"  # explicit snapshot: as asked


def test_fallback_marks_the_snapshot_recently_used() -> None:
    store = SnapshotStore(capacity=2)
    first = session_snapshot(store, button("A", 0, "a"), pid=1)
    session_snapshot(store, button("B", 0, "b"), pid=2)
    store.resolve(ref_of(first, "A"))  # first is now the most recently used
    session_snapshot(store, button("C", 0, "c"), pid=3)
    assert first.snapshot_id in store


def test_gone_and_never_issued_refs_are_stale() -> None:
    store = SnapshotStore(capacity=1)
    first = session_snapshot(store, button("A", 0, "a"), button("B", 70, "b"))
    session_snapshot(store, button("A", 0, "a"))  # evicts first; B is gone
    error = expect(ErrorCode.STALE_REF, store.resolve, ref_of(first, "B"))
    assert "not listed in any recent snapshot" in error.message
    assert error.hint is not None
    assert "new snapshot" in error.hint
    error = expect(ErrorCode.STALE_REF, store.resolve, "e400")
    assert error.message == "Ref e400 was never issued in this session (the highest so far is e2)."


def test_never_issued_message_without_any_ref_of_that_kind() -> None:
    store = SnapshotStore()
    session_snapshot(store, button("A", 0, "a"))
    error = expect(ErrorCode.STALE_REF, store.resolve, "o4")
    assert error.message == "Ref o4 was never issued in this session."


def test_an_element_relisted_under_another_ref_makes_the_old_ref_stale() -> None:
    def list_row(text: str) -> RawNode:
        return RawNode(role="AXRow", title=text, frame=Rect(0, 100, 300, 20), handle="row-view")

    store = SnapshotStore()
    first = session_snapshot(store, list_row("Item 5"), button("Other", 400, "other"))
    # the table recycled the same row view for other content: the row gets a new ref …
    later = session_snapshot(store, list_row("Item 25"))
    assert ref_of(later, "Item 25") != ref_of(first, "Item 5")
    # … so the old ref must not reach the recycled row through the older snapshot
    error = expect(ErrorCode.STALE_REF, store.resolve, ref_of(first, "Item 5"))
    assert f"lists that element as {ref_of(later, 'Item 25')}" in error.message
    assert error.hint is not None
    assert "by its name" in error.hint
    assert store.resolve(ref_of(first, "Other"))[0] is first  # unrelated old refs still resolve


def test_elements_without_handles_resolve_through_older_snapshots() -> None:
    store = SnapshotStore()
    unhandled = RawNode(role="AXButton", title="Floating", frame=Rect(0, 10, 60, 20), actions=("AXPress",))
    first = session_snapshot(store, unhandled, button("Stays", 100, "stays"))
    session_snapshot(store, button("Stays", 100, "stays"))
    snap, el = store.resolve(ref_of(first, "Floating"))
    assert (snap, el.handle) == (first, None)


def test_ocr_refs_expire_when_their_window_is_captured_again() -> None:
    store = SnapshotStore()
    root = RawNode(role="AXWindow", frame=Rect(0, 0, 800, 600), handle="w")
    app = AppInfo("Canvas", 5, "com.example.canvas")
    window = WindowInfo(9, 5, "Canvas", "Art", Rect(0, 0, 800, 600))
    raw = build_snapshot(RawTree(app, window, root), snapshot_id=store.new_id())
    lines = [OcrLine(text="Brush", confidence=0.9, frame=Rect(100, 100, 60, 14))]
    vision = merge_vision(
        carry_refs(raw, None, store.refs), ocr_elements(lines), allocate=store.refs.allocator("o")
    )
    store.put(vision)
    assert store.resolve("o1")[1].name == "Brush"
    session_snapshot(store, button("Other app", 0, "x"), pid=6)
    assert store.resolve("o1")[1].name == "Brush"  # another app's snapshot changes nothing
    again = carry_refs(
        build_snapshot(RawTree(app, window, root), snapshot_id=store.new_id()), vision, store.refs
    )
    store.put(again)
    error = expect(ErrorCode.STALE_REF, store.resolve, "o1")
    assert "OCR text" in error.message
    assert again.snapshot_id in error.message
    assert error.hint is not None
    assert "vision" in error.hint
    assert store.resolve(f"{vision.snapshot_id}:o1")[1].name == "Brush"  # explicit snapshot: as asked


def test_put_reserves_plain_refs_and_refuses_foreign_sessions() -> None:
    store = SnapshotStore()
    plain = make_snapshot(store, buttons=4)
    assert plain.ref_session is None
    assert store.refs.next() == "e5"
    foreign = carry_refs(plain, None, RefCounter())
    with pytest.raises(ValueError, match="another session"):
        store.put(foreign)
    shared_counter = RefCounter()
    shared = SnapshotStore(refs=shared_counter)
    shared.put(carry_refs(plain, None, shared_counter))
    assert len(shared) == 1


def test_snapshots_of_lists_one_window_newest_first() -> None:
    store = SnapshotStore()
    a1 = session_snapshot(store, button("A", 0, "a"), pid=1, window_id=7)
    session_snapshot(store, button("B", 0, "b"), pid=1, window_id=8)
    session_snapshot(store, button("C", 0, "c"), pid=2, window_id=7)
    a2 = session_snapshot(store, button("A", 0, "a"), pid=1, window_id=7)
    no_window = session_snapshot(store, button("D", 0, "d"), pid=1, window_id=None)
    assert store.snapshots_of(1, 7) == [a2, a1]
    assert store.snapshots_of(1, 7, limit=1) == [a2]
    assert store.snapshots_of(1, None) == [no_window]
    assert store.snapshots_of(3, 7) == []
    assert ref_of(a2, "A") == ref_of(a1, "A")


def test_describe_refs_compresses_runs() -> None:
    store = SnapshotStore()
    snap = make_snapshot(store, buttons=3)
    assert describe_refs(snap) == "e1–e3"
    for el, ref in zip(snap.all_elements, ["e4", "e9", "e5"], strict=True):
        el.ref = ref
    assert describe_refs(snap) == "e4–e5, e9"
    snap.all_elements[0].ref = "o2"
    assert describe_refs(snap) == "e5, e9, o2"
    snap.all_elements[1].ref = "not-a-ref"
    assert describe_refs(snap) == "e5, o2"
    many = make_snapshot(store, buttons=20)
    for index, el in enumerate(many.all_elements):
        el.ref = f"e{index * 2 + 1}"
    assert describe_refs(many).endswith(", …")
    assert describe_refs(make_snapshot(store, buttons=0)) == "no elements"


def test_clear_and_capacity_validation() -> None:
    store = SnapshotStore()
    make_snapshot(store)
    store.clear()
    assert len(store) == 0
    assert store.latest() is None
    with pytest.raises(ValueError, match="capacity"):
        SnapshotStore(capacity=0)


def test_snapshot_store_is_thread_safe() -> None:
    store = SnapshotStore(capacity=16)
    errors: list[BaseException] = []
    seen: list[str] = []
    seen_lock = threading.Lock()

    def worker() -> None:
        try:
            for _ in range(100):
                snap = make_snapshot(store)
                with seen_lock:
                    seen.append(snap.snapshot_id)
                try:
                    store.resolve("e1", snap.snapshot_id)
                except AXPilotError as exc:
                    if exc.code is not ErrorCode.SNAPSHOT_NOT_FOUND:  # eviction in between is fine
                        raise
                store.latest()
        except BaseException as exc:  # pragma: no cover - only on failure
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(seen) == len(set(seen)) == 800
    assert len(store) == 16


# --------------------------------------------------------------------------- capture store
def test_capture_store_put_get_latest() -> None:
    store = CaptureStore()
    first_id = store.put(make_capture(10))
    second_id = store.put(make_capture(20))
    assert CAPTURE_ID.match(first_id)
    assert first_id != second_id
    assert store.get(first_id).width == 10
    assert store.get(None).width == 20


def test_capture_store_errors_and_eviction() -> None:
    store = CaptureStore(capacity=2)
    expect(ErrorCode.SNAPSHOT_NOT_FOUND, store.get, None)
    ids = [store.put(make_capture(i + 1)) for i in range(3)]
    error = expect(ErrorCode.SNAPSHOT_NOT_FOUND, store.get, ids[0])
    assert "Screenshot" in error.message
    assert store.get(ids[2]).width == 3
    assert len(store) == 2
    with pytest.raises(ValueError, match="capacity"):
        CaptureStore(capacity=0)


def test_capture_store_is_thread_safe() -> None:
    store = CaptureStore(capacity=8)
    ids: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(50):
            capture_id = store.put(make_capture())
            with lock:
                ids.append(capture_id)
            store.get(None)
            time.sleep(0)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(ids) == len(set(ids)) == 300
    assert len(store) == 8

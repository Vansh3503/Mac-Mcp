from __future__ import annotations

import re
import threading
import time

import pytest

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import AppInfo, Capture, RawNode, RawTree, Rect
from axpilot.perception import CaptureStore, Snapshot, SnapshotStore, build_snapshot, parse_ref
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
    assert "e1–e3" in error.message
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

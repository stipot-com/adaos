import pytest

from adaos.services.yjs.store import (
    add_ystore_write_listener,
    get_ystore_for_webspace,
    suppress_ystore_write_notifications,
    ystore_write_metadata,
)

pytestmark = pytest.mark.anyio


async def test_ystore_write_listener_suppression():
    seen: list[tuple[str, bytes]] = []

    def _cb(webspace_id: str, update: bytes) -> None:
        seen.append((webspace_id, update))

    remove = add_ystore_write_listener(_cb)
    try:
        store = get_ystore_for_webspace("default")
        await store.start()
        await store.write(b"a")
        assert seen and seen[-1] == ("default", b"a")

        seen.clear()
        async with suppress_ystore_write_notifications():
            await store.write(b"b")
        assert seen == []
    finally:
        remove()


async def test_ystore_write_listener_receives_optional_metadata():
    seen: list[tuple[str, bytes, dict[str, object]]] = []

    def _cb(webspace_id: str, update: bytes, meta: dict[str, object]) -> None:
        seen.append((webspace_id, update, dict(meta)))

    remove = add_ystore_write_listener(_cb)
    try:
        store = get_ystore_for_webspace("default")
        await store.start()
        async with ystore_write_metadata(root_names=["data"], source="test_case"):
            await store.write(b"x")
        assert seen
        webspace_id, update, meta = seen[-1]
        assert webspace_id == "default"
        assert update == b"x"
        assert meta["root_names"] == ["data"]
        assert meta["source"] == "test_case"
    finally:
        remove()


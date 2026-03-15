import pytest

from adaos.services.yjs.store import (
    add_ystore_write_listener,
    get_ystore_for_webspace,
    suppress_ystore_write_notifications,
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


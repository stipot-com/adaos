from __future__ import annotations

import time

import pytest

from adaos.services.root.ids import (
    TimestampedId,
    generate_device_id,
    generate_event_id,
    generate_node_id,
    generate_subnet_id,
    generate_trace_id,
    uuid7,
)


def test_uuid7_monotonicity():
    ts = time.time()
    first = uuid7(ts)
    second = uuid7(ts + 0.001)
    assert first.int < second.int


def test_generators_return_uuid_strings():
    ids = [
        generate_subnet_id(),
        generate_node_id(),
        generate_device_id(),
        generate_event_id(),
        generate_trace_id(),
    ]
    for value in ids:
        assert isinstance(value, str)
        assert len(value) == 36  # canonical uuid string length


@pytest.mark.parametrize("factory", [TimestampedId.with_uuid7])
def test_timestamped_id_provides_metadata(factory):
    created = factory()
    assert created.value
    assert created.created_at.endswith("+00:00")

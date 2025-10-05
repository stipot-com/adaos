from __future__ import annotations

from datetime import timedelta

from adaos.services.root.enums import ConsentStatus, ConsentType, DeviceRole, Scope
from adaos.services.root.ids import (
    generate_device_id,
    generate_event_id,
    generate_node_id,
    generate_subnet_id,
    generate_trace_id,
)
from adaos.services.root.models import AuditEvent, ConsentRequest, DeviceRecord, NodeRecord, SubnetRecord


def _device_id() -> str:
    return generate_device_id()


def _subnet_id() -> str:
    return generate_subnet_id()


def _node_id() -> str:
    return generate_node_id()


def test_device_record_scope_management():
    record = DeviceRecord(
        id=_device_id(),
        role=DeviceRole.MEMBER,
        subnet_id=_subnet_id(),
    )
    record.grant_scopes([Scope.EMIT_EVENT, Scope.SUBSCRIBE_EVENT])
    record.grant_scopes([Scope.EMIT_EVENT])  # duplicate is ignored
    assert record.scopes == [Scope.EMIT_EVENT, Scope.SUBSCRIBE_EVENT]
    record.revoke_scope(Scope.EMIT_EVENT)
    assert record.scopes == [Scope.SUBSCRIBE_EVENT]


def test_device_record_aliases_are_unique():
    record = DeviceRecord(id=_device_id(), role=DeviceRole.HUB, subnet_id=_subnet_id())
    record.add_alias("primary")
    record.add_alias("primary")
    assert record.aliases == ["primary"]


def test_consent_request_expiration():
    request = ConsentRequest(
        id="consent-1",
        consent_type=ConsentType.DEVICE,
        requester_id=_device_id(),
        subnet_id=_subnet_id(),
        scopes_requested=[Scope.MANAGE_MEMBERS],
        ttl=timedelta(seconds=1),
    )
    assert request.status == ConsentStatus.PENDING
    assert request.expires_at > request.created_at


def test_audit_event_as_dict_contains_required_fields():
    event = AuditEvent(
        id=generate_event_id(),
        trace_id=generate_trace_id(),
        subnet_id=_subnet_id(),
        actor_id=_device_id(),
        subject_id=_node_id(),
        action="device.revoke",
        acl=[Scope.READ_LOGS],
        ttl=timedelta(minutes=5),
        payload={"reason": "manual"},
    )
    data = event.as_dict()
    assert data["event_id"] == event.id
    assert data["action"] == "device.revoke"
    assert data["acl"] == [scope.value for scope in event.acl]
    assert data["ttl"] == 300

import importlib
from types import SimpleNamespace

import pytest


@pytest.mark.anyio
async def test_pipeline_routes_to_neural_when_flag_enabled(monkeypatch):
    monkeypatch.setenv("ADAOS_NLU_NEURAL", "1")
    from adaos.services.nlu import pipeline

    module = importlib.reload(pipeline)

    emitted = []

    async def _no_regex(_text: str, *, webspace_id: str):
        return (None, {}, "regex", {})

    monkeypatch.setattr(module, "_seen_recent", lambda _rid: False)
    monkeypatch.setattr(module, "_try_regex_intent", _no_regex)
    monkeypatch.setattr(module, "get_ctx", lambda: SimpleNamespace(bus=object()))
    monkeypatch.setattr(module, "bus_emit", lambda _bus, et, payload, source=None: emitted.append((et, payload, source)))

    await module._on_detect_request({"text": "непонятный запрос", "webspace_id": "ws1", "request_id": "rid1"})

    assert emitted
    event_type, payload, source = emitted[-1]
    assert event_type == "nlp.intent.detect.neural"
    assert payload["text"] == "непонятный запрос"
    assert payload["webspace_id"] == "ws1"
    assert payload["request_id"] == "rid1"
    assert source == "nlu.pipeline"


@pytest.mark.anyio
async def test_pipeline_routes_to_rasa_when_flag_disabled(monkeypatch):
    monkeypatch.setenv("ADAOS_NLU_NEURAL", "0")
    from adaos.services.nlu import pipeline

    module = importlib.reload(pipeline)

    emitted = []

    async def _no_regex(_text: str, *, webspace_id: str):
        return (None, {}, "regex", {})

    monkeypatch.setattr(module, "_seen_recent", lambda _rid: False)
    monkeypatch.setattr(module, "_try_regex_intent", _no_regex)
    monkeypatch.setattr(module, "get_ctx", lambda: SimpleNamespace(bus=object()))
    monkeypatch.setattr(module, "bus_emit", lambda _bus, et, payload, source=None: emitted.append((et, payload, source)))

    await module._on_detect_request({"text": "another", "webspace_id": "ws2", "request_id": "rid2"})

    assert emitted
    event_type, payload, source = emitted[-1]
    assert event_type == "nlp.intent.detect.rasa"
    assert payload["text"] == "another"
    assert payload["webspace_id"] == "ws2"
    assert payload["request_id"] == "rid2"
    assert source == "nlu.pipeline"

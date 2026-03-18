import json
import logging


def _mk_record(name: str = "adaos.eventbus") -> logging.LogRecord:
    r = logging.LogRecord(
        name=name,
        level=logging.DEBUG,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    # Make it deterministic.
    r.created = 1700000000.25
    return r


def test_services_json_formatter_includes_time() -> None:
    from adaos.services.logging import JsonFormatter

    rec = _mk_record()
    line = JsonFormatter().format(rec)
    obj = json.loads(line)
    assert obj.get("time"), obj
    assert isinstance(obj.get("time"), str)
    assert obj.get("ts") == rec.created


def test_sdk_json_formatter_includes_time() -> None:
    from adaos.sdk.core.logging import JsonFormatter

    rec = _mk_record("adaos.scenario.demo")
    line = JsonFormatter().format(rec)
    obj = json.loads(line)
    assert obj.get("time"), obj
    assert isinstance(obj.get("time"), str)
    assert obj.get("ts") == rec.created


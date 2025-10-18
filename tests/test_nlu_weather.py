from adaos.services.nlu.context import DialogContext
from adaos.services.nlu.arbiter import arbitrate


def test_weather_today_then_london(monkeypatch):
    def fake_today(text, today=None):
        return "2025-10-18" if "сегодня" in text else None

    monkeypatch.setattr("adaos.services.nlu.normalizer.normalize_date_ru", fake_today)

    ctx = DialogContext()
    home = "Berlin, DE"

    r1 = arbitrate("какая погода на сегодня?", "ru", home, ctx)
    chosen1 = r1["payload"]["chosen"]
    assert chosen1["intent"] == "weather.show"
    assert chosen1["slots"]["date"] == "2025-10-18"
    assert chosen1["slots"]["place"] == "Berlin, DE"

    r2 = arbitrate("а в лондоне?", "ru", home, ctx)
    chosen2 = r2["payload"]["chosen"]
    assert chosen2["intent"] == "weather.show"
    assert chosen2["slots"]["date"] == "2025-10-18"
    assert chosen2["slots"]["place"] == "London, GB"


def test_weather_without_home_city_asks_place():
    ctx = DialogContext()
    r = arbitrate("погода завтра", "ru", None, ctx)
    assert r["payload"]["chosen"]["slots"].get("date")

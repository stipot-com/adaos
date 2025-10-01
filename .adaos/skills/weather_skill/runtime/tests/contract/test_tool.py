from handlers.main import get_weather

if __name__ == "__main__":
    result = get_weather(city="Test City")
    if not isinstance(result, dict):
        raise SystemExit("result must be a dict")
    if "ok" not in result:
        raise SystemExit("result missing 'ok' field")
    raise SystemExit(0)

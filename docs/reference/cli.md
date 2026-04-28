# CLI

```bash
adaos --help
adaos skill --help
adaos scenario --help
adaos api serve --host 127.0.0.1 --port 8777
```

Local API notes:

- `adaos api serve` runs the local API directly, without the supervisor.
- If `--port` is passed explicitly, AdaOS persists the resulting local address as `local_api_url` in `.adaos/node.yaml`.
- Later `adaos api serve` runs reuse that persisted local port unless you pass another explicit one.
- `8777` and `8778` are the browser-discoverable local hub ports.
- Use a port such as `8779` if you want the web client to stay on Root instead of auto-attaching to the local runtime.

Example calls:

```bash
adaos scenario create demo-scenario
adaos skill run weather_skill weather.get --event --entities '{"city":"Berlin"}'
adaos scenario run greet_on_boot
```

The service module `adaos.services.skill.runtime` provides the same operations for programmatic Python usage.

```python
from adaos.services.skill.runtime import run_skill_handler_sync

run_skill_handler_sync("weather_skill", "weather.get", {"city": "Berlin"})
```

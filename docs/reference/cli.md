# CLI

```bash
adaos --help
adaos skill --help
adaos scenario --help
adaos api serve --host 127.0.0.1 --port 8777
````

пример вызова:

```bash
adaos scenario create demo-scenario
adaos skill run weather_skill weather.get --event --entities '{"city":"Berlin"}'
adaos scenario run greet_on_boot
```

Сервисный модуль `adaos.services.skill.runtime` предоставляет те же операции для
программного использования из Python.

```python
from adaos.services.skill.runtime import run_skill_handler_sync

run_skill_handler_sync("weather_skill", "weather.get", {"city": "Berlin"})
```

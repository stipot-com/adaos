# MCP SQLite DB in Docker

В архиве есть готовая SQLite-база `mcp.db`, а также Docker-конфигурация, которая может заново создать ту же структуру и синтетические данные.

## Состав
- `mcp.db` — готовая база данных
- `schema.sql` — DDL-схема
- `init_db.py` — генерация синтетических данных
- `queries.sql` — проверочные SQL-запросы
- `Dockerfile`
- `docker-compose.yml`

## Запуск
```bash
docker compose up --build -d
```

После запуска база будет доступна в файле:
```bash
./data/mcp.db
```

## Как выполнить запросы
Если у тебя установлен sqlite3 локально:
```bash
sqlite3 ./data/mcp.db < queries.sql
```

Либо внутри контейнера:
```bash
docker exec -it mcp-db python - <<'PY'
import sqlite3
conn = sqlite3.connect('/data/mcp.db')
cur = conn.cursor()
for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"):
    print(row)
conn.close()
PY
```

## Что есть в базе
- 4 роли
- 60 пользователей
- 4 статуса сессий
- 3 типа токенов
- 8 типов действий
- синтетические сессии, токены и журнал действий

## Примечание
В Docker здесь используется SQLite. Это удобно для учебного проекта и проверки запросов, но для многопользовательской промышленной эксплуатации такой вариант слабее серверных СУБД.

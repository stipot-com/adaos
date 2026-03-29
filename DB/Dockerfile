FROM python:3.12-slim

WORKDIR /app

COPY schema.sql /app/schema.sql
COPY init_db.py /app/init_db.py
COPY queries.sql /app/queries.sql

ENV DB_PATH=/data/mcp.db

RUN mkdir -p /data

CMD ["sh", "-c", "python /app/init_db.py && echo 'Container is ready. DB: /data/mcp.db' && tail -f /dev/null"]

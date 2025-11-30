from __future__ import annotations

from .core import (
    CodeMapError,
    list_entries,
    search_entries,
)
from .models import CodeEntry


def main() -> None:
    try:
        entries = list_entries(limit=5)
    except CodeMapError as e:
        print(f"Ошибка загрузки карты: {e}")
        return

    print(f"Всего примеров (до 5): {len(entries)}")
    if entries:
        print("Пример записи:", entries[0])

    query = "service"  
    matched: list[CodeEntry] = search_entries(query, limit=5)
    print(f"Совпадений по '{query}': {len(matched)}")
    for e in matched:
        print(" -", e.path)


if __name__ == "__main__":
    main()

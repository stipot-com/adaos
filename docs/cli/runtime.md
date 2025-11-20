# adaos runtime

Команды для управления средой выполнения навыков (slots, meta, rollback).

---

## Структура среды

````

skills/.runtime/<name>/<version>/
slots/
A/
src/
vendor/
runtime/
B/
active
previous
meta.json

````

---

## Основные команды

### Проверка статуса

```bash
adaos runtime status <SKILL_NAME>
````

Показывает активный слот (`A` или `B`) и сведения о версии.

### Активация слота

```bash
adaos runtime activate <SKILL_NAME> --slot A|B
```

Переключает активный слот. При успехе обновляет файл `active` и делает резервную копию в `previous`.

### Откат

```bash
adaos runtime rollback <SKILL_NAME>
```

Возвращает систему к предыдущему здоровому слоту (`previous` → `active`).

---

## Пример

```bash
adaos runtime activate weather-skill --slot B
adaos runtime status weather-skill
```

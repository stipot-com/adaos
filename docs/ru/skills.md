# Навыки

## Что такое навык

В текущем AdaOS runtime навык - это управляемая единица, которую можно scaffold'ить, валидировать, устанавливать, обновлять, активировать и, в некоторых случаях, супервизировать как долгоживущий сервис.

## Базовые команды

```bash
adaos skill list
adaos skill create my_skill
adaos skill validate my_skill
adaos skill install my_skill
adaos skill migrate
adaos skill activate my_skill
adaos skill rollback my_skill
```

## Runtime-ориентированные команды

```bash
adaos skill run my_skill --topic some.topic --payload '{}'
adaos skill setup my_skill
adaos skill status
adaos skill doctor my_skill
adaos skill gc
```

## Service-type skills

Часть навыков публикуется через service supervisor:

```bash
adaos skill service list
adaos skill service status <name>
adaos skill service restart <name>
```

Этот путь обслуживается endpoint'ами `/api/services/*`.

## Разделение publish flow

Теперь в репозитории разделены:

- workspace git push-команды, например `adaos skill push --message ...`
- Root-backed developer publish flow через `adaos dev skill ...`

Это важное отличие по сравнению со старой документацией.

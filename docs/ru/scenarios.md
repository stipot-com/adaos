# Сценарии

## Что такое сценарий

Сценарий - это управляемый workflow-артефакт, который можно устанавливать в workspace, валидировать, исполнять и тестировать.

## Базовые команды

```bash
adaos scenario list
adaos scenario install my_scenario
adaos scenario validate my_scenario
adaos scenario run my_scenario
adaos scenario test my_scenario
adaos scenario status
```

## Workspace и dev space

Текущая реализация поддерживает сравнение статуса в двух режимах:

- `workspace`: сравнение с registry-репозиторием workspace
- `dev`: сравнение локальных draft-версий с Root-backed draft state

Примеры:

```bash
adaos scenario status
adaos scenario status my_scenario --fetch --diff
adaos scenario status --space dev
```

## Связь с webspace

Сценарии также участвуют в Yjs webspace runtime:

- у webspace может быть home scenario
- оператор может переключать активный сценарий для webspace
- desktop state и scenario state связаны в `node yjs` control surface

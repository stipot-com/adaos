# src/adaos/apps/cli/commands/interpreter.py
from __future__ import annotations

import json
from typing import List, Optional

import typer

from adaos.apps.bootstrap import get_ctx
from adaos.services.interpreter.workspace import IntentMapping, InterpreterWorkspace
from adaos.services.interpreter.trainer import RasaTrainer
from adaos.services.interpreter.registry import sync_from_scenarios_and_skills
from adaos.services.interpreter.runtime import RasaNLURuntime

app = typer.Typer(help="Интерпретатор: конфигурация, датасеты и обучение.")
intent_app = typer.Typer(help="Работа с интентами интерпретатора.")
dataset_app = typer.Typer(help="Предустановленные датасеты.")
app.add_typer(intent_app, name="intent")
app.add_typer(dataset_app, name="dataset")


def _workspace() -> InterpreterWorkspace:
    ctx = get_ctx()
    return InterpreterWorkspace(ctx)


@app.command("sync-nlu")
def sync_nlu() -> None:
    """
    Синхронизировать NLU-данные из skill.yaml/skill metadata и scenario.json['nlu']
    в рабочее пространство интерпретатора (config.yaml + datasets).
    """
    ctx = get_ctx()
    summary = sync_from_scenarios_and_skills(ctx)
    typer.echo(
        f"NLU sync: skills_intents={summary['skills_intents']} "
        f"scenario_intents={summary['scenario_intents']}"
    )


@app.command("parse")
def parse(
    text: str = typer.Argument(..., help="Текст, который нужно разобрать интерпретатором."),
) -> None:
    """
    Прогоняет текст через последнюю обученную модель интерпретатора (Rasa)
    и печатает результат распознавания (intent, confidence, slots).
    """
    ws = _workspace()
    runtime = RasaNLURuntime(ws)
    result = runtime.parse(text)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("status")
def status(show_skills: bool = typer.Option(False, "--show-skills", help="Показать снимок установленных скиллов.")) -> None:
    """Показывает актуальность обучения и количество данных."""
    ws = _workspace()
    info = ws.describe_status()
    typer.echo(f"Интентов: {info['intent_count']}")
    typer.echo(f"Файлов датасета: {len(info['dataset_summary'].get('files', []))}")
    if info["needs_training"]:
        typer.secho("Нужно переобучить модель:", fg=typer.colors.YELLOW)
        for reason in info["reasons"]:
            typer.echo(f"- {reason}")
    else:
        typer.secho(f"Модель актуальна (обучена {info.get('trained_at')})", fg=typer.colors.GREEN)
    if show_skills and info["skills"]:
        typer.echo("Скиллы в снапшоте:")
        for meta in info["skills"]:
            typer.echo(f"  - {meta['id']} v{meta.get('version')}")


@app.command("train")
def train(
    note: Optional[str] = typer.Option(None, "--note", help="Комментарий к запуску."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Показать статус и выйти."),
    engine: str = typer.Option("rasa", "--engine", help="Движок обучения (rasa/none)."),
) -> None:
    """Запускает обучение и сохраняет артефакт/метаданные."""
    ws = _workspace()
    if dry_run:
        typer.echo(json.dumps(ws.describe_status(), ensure_ascii=False, indent=2))
        return

    if engine == "rasa":
        trainer = RasaTrainer(ws)
        meta = trainer.train(note=note)
        model_path = meta.get("extra", {}).get("model_path")
        typer.secho(f"Rasa-модель обучена: {meta['trained_at']}", fg=typer.colors.GREEN)
        if model_path:
            typer.echo(f"Артефакт: {model_path}")
    else:
        meta = ws.record_training(note=note or "manual")
        typer.secho(f"Состояние зафиксировано: {meta['trained_at']}", fg=typer.colors.GREEN)
    typer.echo("Хэш состояния: " + meta["fingerprint"])


@intent_app.command("list")
def list_intents() -> None:
    ws = _workspace()
    mappings = ws.list_intents()
    if not mappings:
        typer.echo("Интенты не заданы.")
        return
    typer.echo("intent | target | description")
    typer.echo("-" * 60)
    for item in mappings:
        target = "—"
        if item.skill and item.tool:
            target = f"{item.skill}:{item.tool}"
        elif item.skill:
            target = item.skill
        elif item.scenario:
            target = f"scenario:{item.scenario}"
        typer.echo(f"{item.intent} | {target} | {item.description or ''}")


@intent_app.command("add")
def add_intent(
    intent: str = typer.Argument(..., help="Имя интента."),
    skill: Optional[str] = typer.Option(None, "--skill", help="Скилл, который следует вызвать."),
    tool: Optional[str] = typer.Option(None, "--tool", help="Публичный инструмент внутри скилла."),
    scenario: Optional[str] = typer.Option(None, "--scenario", help="Сценарий вместо скилла."),
    description: Optional[str] = typer.Option(None, "--description", "-d", help="Описание."),
    examples: List[str] = typer.Option(None, "--example", "-e", help="Примеры (можно повторять)."),
) -> None:
    if not (skill or scenario):
        raise typer.BadParameter("Нужно указать --skill или --scenario.")
    mapping = IntentMapping(
        intent=intent.strip(),
        description=description,
        skill=skill,
        tool=tool,
        scenario=scenario,
        examples=examples or [],
    )
    ws = _workspace()
    ws.upsert_intent(mapping)
    typer.secho(f"Интент '{intent}' сохранён.", fg=typer.colors.GREEN)


@intent_app.command("remove")
def remove_intent(intent: str = typer.Argument(..., help="Имя интента.")) -> None:
    ws = _workspace()
    if ws.remove_intent(intent):
        typer.secho(f"Интент '{intent}' удалён.", fg=typer.colors.GREEN)
    else:
        typer.echo(f"Интент '{intent}' не найден.")
        raise typer.Exit(code=1)


@dataset_app.command("list")
def dataset_list() -> None:
    ws = _workspace()
    presets = ws.available_presets()
    if not presets:
        typer.echo("Пакетные датасеты не найдены.")
        return
    typer.echo("Доступные пресеты:")
    for name in presets:
        typer.echo(f"- {name}")


@dataset_app.command("sync")
def dataset_sync(
    preset: str = typer.Option("moodbot", "--preset", help="Имя пресета из пакета."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Перезаписать существующие файлы."),
) -> None:
    ws = _workspace()
    path = ws.sync_dataset_from_repo(preset, overwrite=overwrite)
    typer.echo(f"Датасет '{preset}' синхронизирован: {path}")


@app.command("init-skill")
def init_skill(
    name: str = typer.Argument("interpreter_router", help="Имя скилла-диспетчера."),
    template: str = typer.Option("skill_default", "--template", help="Шаблон скилла."),
) -> None:
    """Создаёт базовый скилл, который можно использовать как диспетчер."""
    from adaos.services.skill import scaffold

    ws = _workspace()
    try:
        skill_path = scaffold.create(name, template=template, register=False, push=False)
    except FileNotFoundError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    typer.secho(f"Скилл '{name}' создан: {skill_path}", fg=typer.colors.GREEN)
    typer.echo(f"Конфигурация хранится в {ws.config_path}")


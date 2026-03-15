# tests/test_cli_basic.py
import json

from typer.testing import CliRunner


def test_cli_help(cli_app):
    r = CliRunner().invoke(cli_app, ["--help"])
    assert r.exit_code == 0
    assert "Usage" in r.stdout or "РёСЃРїРѕР»СЊР·РѕРІР°РЅРёРµ" in r.stdout.lower()


def test_repo_registry_list_json(cli_app, tmp_base_dir):
    workspace = tmp_base_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "registry.json").write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "2026-03-06T00:00:00+00:00",
                "skills": [{"kind": "skill", "name": "weather_skill", "version": "1.0.0"}],
                "scenarios": [],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli_app, ["repo", "registry", "list", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["items"][0]["name"] == "weather_skill"

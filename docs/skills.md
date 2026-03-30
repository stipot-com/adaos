# Skills

## What a skill is

In the current AdaOS runtime, a skill is a managed unit that can be scaffolded, validated, installed, updated, activated, and in some cases supervised as a long-running service.

## Common commands

```bash
adaos skill list
adaos skill create my_skill
adaos skill validate my_skill
adaos skill install my_skill
adaos skill migrate
adaos skill activate my_skill
adaos skill rollback my_skill
```

## Runtime-oriented commands

```bash
adaos skill run my_skill --topic some.topic --payload '{}'
adaos skill setup my_skill
adaos skill status
adaos skill doctor my_skill
adaos skill gc
```

## Service-type skills

Some skills are exposed through the service supervisor:

```bash
adaos skill service list
adaos skill service status <name>
adaos skill service restart <name>
```

This path is backed by `/api/services/*`.

## Publishing split

The repository now distinguishes between:

- workspace git push commands such as `adaos skill push --message ...`
- Root-backed developer publishing through `adaos dev skill ...`

That split is important when reading older documentation.

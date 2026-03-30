# Scenarios

## What a scenario is

A scenario is a managed workflow artifact that can be installed into the workspace, validated, executed, and tested.

## Common commands

```bash
adaos scenario list
adaos scenario install my_scenario
adaos scenario validate my_scenario
adaos scenario run my_scenario
adaos scenario test my_scenario
adaos scenario status
```

## Workspace versus dev space

The current implementation supports status comparison in two modes:

- `workspace`: compare against the workspace registry repository
- `dev`: compare local drafts against Root-backed draft state

Examples:

```bash
adaos scenario status
adaos scenario status my_scenario --fetch --diff
adaos scenario status --space dev
```

## Relation to webspaces

Scenarios are also used by the Yjs webspace runtime:

- a webspace can have a home scenario
- operators can switch the active scenario for a webspace
- desktop state and scenario state are tied together in the node Yjs control surface

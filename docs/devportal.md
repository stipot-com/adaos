# DevPortal

`adaos dev` contains the developer workflows that target Root-backed and Forge-style environments.

## Main command groups

- `adaos dev root`: initialize, log in, and inspect Root-side logs
- `adaos dev skill`: create, push, validate, publish, prep, run, and test owner skills
- `adaos dev scenario`: create, push, validate, publish, run, and test owner scenarios

## Typical flow

```bash
adaos dev root init
adaos dev root login
adaos dev skill create demo_skill
adaos dev skill push demo_skill
adaos dev skill publish demo_skill
```

## Important distinction

These commands are not the same as the local workspace lifecycle:

- `adaos skill ...` and `adaos scenario ...` operate on the local workspace/runtime path
- `adaos dev skill ...` and `adaos dev scenario ...` are for Root-backed developer workflows

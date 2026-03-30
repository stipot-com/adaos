# Agent Context

## Purpose

`AgentContext` is the shared runtime container used by the CLI and API server. It gives commands and services a single source of truth for paths, settings, repositories, event bus access, and capability checks.

## How it is initialized

- the CLI initializes or reloads the context in `src/adaos/apps/cli/app.py`
- the API server initializes it during startup before routers are mounted
- environment variables and `.env` are applied before runtime services start

## What it provides

Typical context members include:

- resolved paths for workspace, state, and runtime directories
- settings loaded from files and environment
- repositories for skills and scenarios
- SQLite access
- git helpers
- event bus access
- capability and security helpers

## Why this design is useful

This keeps local commands and API operations aligned:

- the same repositories and registries are reused
- operational commands can switch between direct execution and API-backed execution
- bootstrapping and environment preparation happen in one place

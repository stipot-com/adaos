# AdaOS

AdaOS is a Python platform for building distributed assistant systems out of skills, scenarios, node services, and local control APIs.

The current repository is the developer and runtime core of the project. It includes:

- a local CLI exposed as `adaos`
- a FastAPI-based control API
- runtime services for node, skill, scenario, and webspace management
- SDK modules for skills, data access, events, and control-plane integration
- bootstrap scripts, tests, and MkDocs documentation

## What AdaOS does today

The current implementation is centered around local and private deployments:

- run a node as a `hub` or `member`
- manage skills and scenarios from the CLI
- expose runtime control over HTTP with token-based local authentication
- operate service-type skills through a supervisor and `/api/services/*`
- manage Yjs-backed webspaces and desktop state through `adaos node yjs ...`
- support subnet onboarding with join codes and member updates
- provide developer workflows for Root and Forge-style publishing

## Main areas

- [Quickstart](quickstart.md): installation, bootstrap, and first commands
- [Architecture](architecture/index.md): how the runtime is organized
- [CLI](cli/index.md): command groups and operational workflows
- [SDK](sdk/index.md): public Python-facing building blocks
- [Skills](skills.md): skill lifecycle and runtime behavior
- [Scenarios](scenarios.md): scenario lifecycle and execution model
- [DevPortal](devportal.md): developer workflows for Root-backed environments

## Current scope

AdaOS already includes real operational features such as node roles, autostart, core update orchestration, service supervision, monitoring, and Yjs webspace control. At the same time, some documents in this repository still describe target-state ideas. The pages outside `Roadmap` and `Concepts` are focused here on the implementation that exists in `src/adaos` today.

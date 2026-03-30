# Security

## Local control API

The local API is protected with the `X-AdaOS-Token` header. The token is typically read from environment or node configuration and is used by CLI commands when they call the control API.

## Node and subnet controls

Current runtime security features include:

- role-aware node configuration
- join-code based member onboarding
- explicit shutdown and drain flows
- member update requests through the hub
- local service and runtime controls gated behind the local API token

## Secret handling

AdaOS includes secret-management commands and adapters for storing secrets outside ordinary source files. The runtime also keeps environment-derived settings separate from repository content where possible.

## Git and workspace safety

Operational commands are designed around managed workspaces and explicit install/update flows instead of arbitrary repository mutation. In practice this reduces the amount of ad hoc filesystem editing required during runtime operations.

## Scope

Some broader trust, PKI, and hosted-service concerns are documented elsewhere in the repository, but the pages outside `Roadmap` and `Concepts` focus on the security model that exists in the current Python runtime and local control surface.

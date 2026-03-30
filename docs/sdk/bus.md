# SDK Bus

AdaOS uses an internal event bus for runtime coordination. SDK-facing helpers expose bus and event utilities through `adaos.sdk.data.bus` and related modules.

## What it is used for

- notifying runtime components about skill or scenario changes
- routing local events between services
- supporting observe and monitoring flows
- bridging some local control actions into event-driven behavior

## Practical guidance

The event bus is part of the current implementation, but most application code should interact with it through existing services or SDK helpers instead of depending on bus internals directly.

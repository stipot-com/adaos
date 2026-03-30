# SDK

The AdaOS SDK is the Python-facing layer used by skills and higher-level runtime code.

## Main areas in the current tree

- `adaos.sdk.manage`: management helpers for skills, scenarios, resources, and environment
- `adaos.sdk.data`: data access helpers for context, memory, events, environment, secrets, and profile data
- `adaos.sdk.scenarios`: runtime and workflow helpers for scenarios
- `adaos.sdk.io`: IO-related helpers
- `adaos.sdk.web`: webspace and desktop helpers
- `adaos.sdk.core`: decorators, context access, errors, exporter, and validation

## Design goals

- keep skill-facing helpers lightweight
- make runtime contracts explicit
- support validation and export of tool metadata
- expose enough structure for local developer tooling and control-plane integration

# SDK Adapters

The SDK sits above concrete adapters. In the current implementation, adapters provide the actual filesystem, database, git, audio, secrets, and SDK bridge behavior used by services.

## Examples in the repository

- filesystem path providers
- SQLite registries and stores
- secure git and workspace helpers
- TTS and STT adapters
- secret backends
- in-process skill context adapters

## Guidance

If you extend AdaOS, prefer adding new infrastructure behavior under `adapters/` and then exposing it through services or SDK helpers rather than coupling new code directly to a command entry point.

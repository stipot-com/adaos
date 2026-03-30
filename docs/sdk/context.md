# SDK Context

SDK code works against a runtime context instead of assuming global process state.

## Current pattern

- runtime code initializes context during CLI or API startup
- SDK helpers read from that context when they need paths, capabilities, bus access, or environment data
- validation raises explicit runtime errors when the context is unavailable

## Why it matters

This makes skill execution and internal tooling more predictable and easier to validate than a free-form global import model.

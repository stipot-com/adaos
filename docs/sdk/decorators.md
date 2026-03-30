# SDK Decorators

AdaOS uses decorators from `adaos.sdk.core.decorators` to attach machine-readable metadata to tool functions.

## Current role

The decorator layer is used to:

- define tool input and output schemas
- attach summaries and stability metadata
- support export for LLM-facing or control-plane descriptions

## Related commands

The CLI exposes SDK export helpers through:

```bash
adaos sdk export
adaos sdk export-all
adaos sdk check
```

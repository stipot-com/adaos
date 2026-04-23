# neural_nlu_service_skill artifacts

Place detector artifacts in one of two ways:

1. Explicit env vars:
   - `ADAOS_NEURAL_MODEL_PATH` -> model state dict (`.pt`)
   - `ADAOS_NEURAL_LABELS_PATH` -> JSON list of intents
   - `ADAOS_NEURAL_VOCAB_PATH` -> JSON char vocabulary

2. Default location (auto-discovered):
   - `<ADAOS_BASE_DIR>/state/nlu/neural/model.pt`
   - `<ADAOS_BASE_DIR>/state/nlu/neural/labels.json`
   - `<ADAOS_BASE_DIR>/state/nlu/neural/vocab.json`

If `ADAOS_BASE_DIR` is not set, fallback base is `~/.adaos`.

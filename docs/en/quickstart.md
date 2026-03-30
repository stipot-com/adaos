# Quickstart

```bash
git clone -b rev2026 https://github.com/stipot/adaos.git
cd adaos

# Optional: private modules for client/backend/infra development
git submodule update --init --recursive \
  src/adaos/integrations/adaos-client \
  src/adaos/integrations/adaos-backend \
  src/adaos/integrations/infra-inimatic

# mac/linux:
bash tools/bootstrap.sh
# windows (PowerShell):
./tools/bootstrap.ps1
. .\.venv\Scripts\Activate.ps1

# API
make api
# API: http://127.0.0.1:8777

# Optional modules
make backend    # requires adaos-backend submodule
make web        # requires adaos-client submodule

adaos --help
```

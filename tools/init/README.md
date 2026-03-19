# AdaOS init scripts

These scripts are meant to be hosted by the frontend as “single download → run”.

Proposed public paths:
- `app.inimatic.com/linux/init.sh`
- `app.inimatic.com/windows/init.ps1`
- `app.inimatic.com/windows/init.bat`

They download the `rev2026` branch (by default) and then run the repo bootstrap:
- Linux: `tools/bootstrap.sh`
- Windows: `tools/bootstrap.ps1`

## One-liners

Linux:

```bash
curl -fsSL https://app.inimatic.com/linux/init.sh | bash -s -- --join-code CODE
```

Windows PowerShell:

```powershell
iwr -useb https://app.inimatic.com/windows/init.ps1 | iex; init.ps1 -JoinCode CODE
```

Windows CMD:

```bat
curl -fsSL -o init.bat https://app.inimatic.com/windows/init.bat && init.bat -JoinCode CODE
```


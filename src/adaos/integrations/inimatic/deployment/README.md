# inimatic deployment

This directory contains the production and development deployment assets for the inimatic integration.  The stack is based on Docker Compose with Traefik acting as the reverse proxy and Let's Encrypt certificate manager.

## Prerequisites

* A host with Docker Engine 24+ and Docker Compose v2.
* Public DNS records that point `api.inimatic.com` and `app.inimatic.com` to the deployment host.
* TCP ports 80 and 443 reachable from the internet (Traefik requires them for the ACME HTTP-01 challenge).
* TLS and CA material placed on the server under `deployment/secrets/` (not committed to git).

## Production deployment

1. Copy `deployment/.env.example` to `deployment/.env` and adjust the values to your environment (Git forge settings, contact e-mail, optional forge credentials, etc.).
2. Upload the TLS bundle to the server:
   * `deployment/secrets/ca.key`
   * `deployment/secrets/ca.crt`
   * `deployment/secrets/server.key`
   * `deployment/secrets/server.crt`
   Ensure each file is owned by the deployment user and has permissions `600`.
3. On the target host, run `docker compose --project-directory . -f deployment/docker-compose.yaml --profile prod up -d --wait` from the root of the inimatic repository or simply execute `deployment/deploy.sh`.
4. Validate the rollout:
   * `https://api.inimatic.com/health` should return `200 OK`.
   * `https://app.inimatic.com/` should render the frontend with a valid Let's Encrypt certificate.

Traefik stores the ACME account data and issued certificates in the `traefik_letsencrypt` Docker volume (`/letsencrypt/acme.json` inside the container).  Renewal is automatic; make sure the host has persistent Docker volumes so the store is not lost between restarts.

Use the helper `Makefile` in this directory for common tasks (set `PROFILE=prod` to target production):

```bash
cd deployment
make PROFILE=prod pull
make PROFILE=prod up
```

## Local development (dev profile)

The dev profile runs Redis, the backend, and the frontend without Traefik.  TLS assets are provided via bind mounts.  Place development certificates under `deployment/dev-secrets/` (the defaults referenced in `.env.example`) or adjust the `DEV_*` variables in `.env` to point to local files.

Start the stack with:

```bash
docker compose --project-directory . -f deployment/docker-compose.yaml -f deployment/docker-compose.pg.override.yml --profile dev up -d
```

This exposes the backend on `http://localhost:3030` and the frontend on `http://localhost:8080`. The override file adds a PostgreSQL service and wires `PG_URL` into the backend for Telegram multi-hub routing.

## CI/CD pipeline

The GitHub Actions workflow `.github/workflows/cd.yml` builds backend and frontend images for every push to the `rev2026` branch touching inimatic sources or deployment assets.  Images are published to GHCR with the tags `latest` and `rev2026-<shortsha>` and then deployed to the production host through SSH.

Configure the following repository secrets for deployment:

* `DEPLOY_HOST` – public hostname or IP of the target server.
* `DEPLOY_USER` – SSH user with permissions to pull and run the stack.
* `DEPLOY_KEY` – private SSH key for the deployment user.
* Optional: `DEPLOY_PORT` – non-default SSH port.
* Optional: define `REPO_DIR` in the remote environment to override the default clone directory (`~/adaos`).

The remote script fetches the latest `rev2026` branch, runs `deployment/deploy.sh`, and prints service status via `docker compose ps`.

## Certificate rotation

Traefik performs automatic certificate issuance and renewal via Let's Encrypt.  The ACME data (`acme.json`) resides inside the `traefik_letsencrypt` volume.  Back up this volume if the host is re-provisioned.  Manual intervention is only required when the contact e-mail or domains change.

## Optional hardening

For additional protection of administrative API endpoints you can enable Traefik's [basic authentication middleware](https://doc.traefik.io/traefik/middlewares/http/basicauth/) by adding a label such as `traefik.http.middlewares.api-basic.basicauth.users=<user>:<hashed-password>` to the backend service and referencing it from the router (`traefik.http.routers.api.middlewares=api-sec@docker,api-rl@docker,api-basic@docker`).

## Troubleshooting

| Symptom | Possible cause | Suggested action |
| ------- | -------------- | ---------------- |
| Let's Encrypt fails to issue certificates | Ports 80/443 blocked, DNS not pointing at host, or previous certificates exhausted the rate limit | Ensure DNS is correct, open ports on firewalls, and wait before retrying when LE rate limits hit. |
| Frontend/API unreachable | Containers unhealthy or Traefik not running | Check `docker compose ps`, inspect logs with `docker compose logs <service>`; validate secrets and `.env` values. |
| ACME storage errors | `traefik_letsencrypt` volume missing or not writable | Remove the container and recreate, ensuring the volume is intact. |
| Port conflicts on startup | Other services listening on 80/443 | Stop conflicting services or adjust host port mappings in the compose file (not recommended for production). |

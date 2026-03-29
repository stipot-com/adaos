# inimatic deployment

This directory contains the Docker Compose deployment assets for `app.inimatic.com`, `api.inimatic.com`, and the related NATS/Redis/Postgres services.

The public edge is built on:

- `nginxproxy/nginx-proxy`
- `nginxproxy/acme-companion`
- Let's Encrypt HTTP-01
- blue/green frontend and backend slots

## Key files

- `docker-compose.yml`: public stack
- `.env.example`: required deployment variables
- `scripts/deploy.sh`: blue/green deploy entrypoint
- `scripts/render_tls_overrides.sh`: renders per-host TLS compatibility snippets
- `vhost.d/`: nginx-proxy server snippets
- `nginx/40-generate-runtime-config.sh`: injects frontend runtime config on container start

## Baseline rollout

1. Copy `.env.example` to `.env` and fill in the real secrets.
2. Keep `NGINX_SSL_POLICY=Mozilla-Intermediate` unless you have a very specific reason to relax the entire proxy.
3. Run `bash scripts/deploy.sh`.
4. Validate:
   - `https://app.inimatic.com/`
   - `https://api.inimatic.com/healthz`
   - `docker exec reverse-proxy nginx -t`

## SmartTV / old browser testing

The deployment now supports targeted legacy TLS overrides per host instead of weakening the whole reverse proxy.

Relevant variables in `.env`:

- `NGINX_SSL_POLICY`: global default TLS policy for nginx-proxy
- `LEGACY_TLS_HOSTS`: comma-separated hostnames that should get an old-client TLS profile
- `LEGACY_TLS_PROTOCOLS`: server-level override, default `TLSv1 TLSv1.1 TLSv1.2 TLSv1.3`
- `LEGACY_TLS_CIPHERS`: server-level override with legacy RSA/CBC suites added

Example:

```env
NGINX_SSL_POLICY=Mozilla-Intermediate
LEGACY_TLS_HOSTS=app.inimatic.com,api.inimatic.com
```

Then deploy:

```bash
bash scripts/deploy.sh
```

`scripts/deploy.sh` calls `scripts/render_tls_overrides.sh`, then reloads nginx inside `reverse-proxy`.

## Interpreting the result on a TV

There are now two separate diagnostics paths:

1. TLS compatibility:
   - If the TV still cannot open the page at all, TLS/SNI/cipher compatibility is still the likely issue.
2. Frontend compatibility:
   - If the TV opens the page and shows a plain "Browser update needed" message, TLS is already working and the blocker is the JS engine / ES module support.

## Frontend runtime config

The frontend container writes `/runtime-config.json` on startup from these env vars:

- `PUBLIC_ROOT_BASE`
- `PUBLIC_ADAOS_BASE`
- `PUBLIC_ADAOS_TOKEN`

`/runtime-config.json` is served with `Cache-Control: no-store`, so changing these values does not fight the service worker cache.

For production, the normal setting is:

```env
PUBLIC_ROOT_BASE=https://api.inimatic.com
PUBLIC_ADAOS_BASE=
PUBLIC_ADAOS_TOKEN=
```

## Frontend browser floor

The current Angular app targets modern browsers only. See `.browserslistrc` in the frontend:

- `Chrome >=79`
- `ChromeAndroid >=79`
- `Firefox >=70`
- `Edge >=79`
- `Safari >=14`
- `iOS >=14`

That means some older SmartTV browsers can pass TLS and still remain incompatible with the frontend bundle.

## Practical action plan

1. Deploy with the current default policy and confirm the baseline behavior.
2. Enable `LEGACY_TLS_HOSTS=app.inimatic.com,api.inimatic.com` and redeploy.
3. Test the TV again.
4. If the TV now reaches the page but shows the legacy-browser notice, stop changing TLS and treat this as a frontend/browser-support issue.
5. If the TV still does not reach the page, check whether the device is missing SNI support or still rejects the offered ciphers.
6. Once the hypothesis is confirmed, either:
   - keep the legacy TLS override only on the required host(s), or
   - move legacy clients to a separate hostname / endpoint so the main site can stay stricter.

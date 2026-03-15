# vhost.d/nats.inimatic.com
client_max_body_size 10m;

# Direct NATS WebSocket passthrough (no mTLS)
#
# Hub-specific `hub_<id>` / `hub_nats_token` credentials are authorized by NATS
# itself via `auth_callout`, so `/nats` can terminate directly on the NATS WS listener.
#
# To keep accidental plain HTTP probes (e.g. `/`) from hitting NATS' WS listener and spamming NATS logs
# with "websocket handshake error: invalid value for header 'Upgrade'", the NATS container's
# `VIRTUAL_PORT` should point at the HTTP monitoring port (8222), not the WS port (8080).
location ^~ /nats {

  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "upgrade";
  proxy_set_header Host $host;
  proxy_set_header Sec-WebSocket-Protocol $http_sec_websocket_protocol;
  proxy_set_header X-Forwarded-Proto https;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

  proxy_read_timeout  3600s;
  proxy_send_timeout  3600s;
  proxy_connect_timeout 10s;

  proxy_buffering off;
  proxy_request_buffering off;

  proxy_pass http://nats:8080;
}

# Let nginx-proxy/acme-companion serve HTTP-01 challenges.
# (Files are written into the shared nginx html volume.)
location ^~ /.well-known/acme-challenge/ {
  try_files $uri =404;
}

# Block everything except `/nats` so the NATS monitoring port (8222) is not exposed publicly via this vhost.
location ~* ^/(?!nats(?:/|$)) {
  return 404;
}

# IMPORTANT: nginx-proxy already generates `location / { ... }` for the upstream container.
# Do NOT add another `location /` here (it causes "duplicate location \"/\"").
# If you want to block accidental plain HTTP probes to the vhost root, use an exact match:
location = / {
  return 404;
}

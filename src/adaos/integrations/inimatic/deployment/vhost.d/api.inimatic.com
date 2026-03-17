# vhost.d/api.inimatic.com

# Allow TLS 1.2 for legacy clients (TLS 1.3 remains enabled).
# ssl_protocols TLSv1.2 TLSv1.3;

ssl_client_certificate /etc/nginx/certs/adaos_ca.pem;
ssl_verify_client optional;
ssl_verify_depth 2;

# --- Noisy health/probe endpoints (keep but don't flood access logs) ---
location = /api/ping {
  access_log off;
  proxy_pass http://api.inimatic.com;
  include /etc/nginx/vhost.d/api.inimatic.com_location;
}

location = /healthz {
  access_log off;
  proxy_pass http://api.inimatic.com;
  include /etc/nginx/vhost.d/api.inimatic.com_location;
}

location = /readyz {
  access_log off;
  proxy_pass http://api.inimatic.com;
  include /etc/nginx/vhost.d/api.inimatic.com_location;
}

location = /metrics {
  access_log off;
  proxy_pass http://api.inimatic.com;
  include /etc/nginx/vhost.d/api.inimatic.com_location;
}

location = /v1/browser/pair/status {
  access_log off;
  proxy_pass http://api.inimatic.com;
  include /etc/nginx/vhost.d/api.inimatic.com_location;
}

# --- Bootstrap endpoints without mTLS ---
location = /v1/bootstrap_token {
  proxy_pass http://api.inimatic.com;
  include /etc/nginx/vhost.d/api.inimatic.com_location;
}

location = /v1/subnets/register {
  proxy_pass http://api.inimatic.com;
  include /etc/nginx/vhost.d/api.inimatic.com_location;
}

location = /v1/pair/confirm {
  proxy_pass http://api.inimatic.com;
  include /etc/nginx/vhost.d/api.inimatic.com_location;
}

# --- Browser -> Hub proxy over Root (WS + HTTP) ---
# Important: only set Upgrade/Connection headers for websocket endpoints.
# Sending `Connection: upgrade` for normal HTTP requests can confuse upstreams and lead to 502/timeouts.
location ~ ^/hubs/[^/]+/(ws|yws/).*$ {
  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "upgrade";
  proxy_set_header Host $host;
  proxy_set_header X-Forwarded-Proto https;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

  proxy_buffering off;
  proxy_request_buffering off;
  proxy_read_timeout 3600s;
  proxy_send_timeout 3600s;
  proxy_connect_timeout 3600s;

  proxy_pass http://api.inimatic.com;
  include /etc/nginx/vhost.d/api.inimatic.com_location;
}

location /hubs/ {
  proxy_set_header Host $host;
  proxy_set_header X-Forwarded-Proto https;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

  proxy_read_timeout 60s;
  proxy_send_timeout 60s;
  proxy_connect_timeout 10s;

  proxy_pass http://api.inimatic.com;
  include /etc/nginx/vhost.d/api.inimatic.com_location;
}

# --- Public NATS-over-WebSocket entrypoint for hubs ---
# Route `/nats` through the backend ws-nats-proxy rather than directly into the
# NATS websocket listener. The hub realtime sidecar speaks raw NATS over a local
# TCP socket and relays that stream over websocket. The backend proxy is the
# component that preserves raw NATS stream semantics for this path.
location ^~ /nats {
  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "upgrade";
  proxy_set_header Host $host;
  proxy_set_header X-Forwarded-Proto https;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  proxy_set_header Sec-WebSocket-Protocol $http_sec_websocket_protocol;

  proxy_read_timeout 3600s;
  proxy_send_timeout 3600s;
  proxy_connect_timeout 10s;
  proxy_buffering off;
  proxy_request_buffering off;

  proxy_pass http://api.inimatic.com;
  include /etc/nginx/vhost.d/api.inimatic.com_location;
}

# --- Protected paths require mTLS ---
location ~ ^/v1/(owner|pki|registry|drafts|devices)/ {
  if ($ssl_client_verify != SUCCESS) { return 400; }
  proxy_pass http://api.inimatic.com;
  include /etc/nginx/vhost.d/api.inimatic.com_location;
}

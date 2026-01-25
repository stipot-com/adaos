# vhost/api.inimatic.com

ssl_client_certificate /etc/nginx/certs/adaos_ca.pem;
ssl_verify_client optional;
ssl_verify_depth 2;

# Pairing confirmation is allowed without mTLS (legacy clients / bootstrap).
location /v1/pair/confirm {
  ssl_verify_client off;
  proxy_set_header X-Forwarded-Proto https;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}

# --- Bootstrap endpoints without mTLS ---
location = /v1/bootstrap_token {
  proxy_pass https://api.inimatic.com;
  include /etc/nginx/vhost.d/api.inimatic.com_location;
}

location = /v1/subnets/register {
  proxy_pass https://api.inimatic.com;
  include /etc/nginx/vhost.d/api.inimatic.com_location;
}

# Telegram webhooks are public and must not require mTLS.
location ^~ /io/tg/webhook/ {
  client_max_body_size 1m;
  if ($request_method !~ ^(POST)$) { return 405; }
  ssl_verify_client off;
  proxy_set_header X-Forwarded-Proto https;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}

location = /healthz { ssl_verify_client off; }

# --- Browser -> Hub proxy over Root (WS + HTTP) ---
location ^~ /hubs/ {
  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "upgrade";
  proxy_set_header Host $host;
  proxy_set_header X-Forwarded-Proto https;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

  proxy_read_timeout 3600s;
  proxy_send_timeout 3600s;
  proxy_connect_timeout 3600s;

  proxy_pass https://api.inimatic.com;
  include /etc/nginx/vhost.d/api.inimatic.com_location;
}

# --- NATS WebSocket passthrough ---
location ^~ /nats {
  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "upgrade";
  proxy_set_header Host $host;
  proxy_set_header Sec-WebSocket-Protocol $http_sec_websocket_protocol;
  proxy_read_timeout 3600s;
  proxy_send_timeout 3600s;
  proxy_connect_timeout 10s;
  proxy_pass https://api.inimatic.com;
  include /etc/nginx/vhost.d/api.inimatic.com_location;
}

# --- Protected paths require mTLS ---
location ~ ^/v1/(owner|pki|registry|drafts|devices)/ {
  if ($ssl_client_verify != SUCCESS) { return 400; }
  proxy_pass http://api.inimatic.com;
  include /etc/nginx/vhost.d/api.inimatic.com_location;
}
